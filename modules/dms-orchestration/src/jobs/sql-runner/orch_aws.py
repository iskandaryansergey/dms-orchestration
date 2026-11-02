#!/usr/bin/env python3
"""The AWS calls of the job runtime on the Python standard library: the runner AMI has python3
without boto3 and without the AWS CLI (SSM runtime, ADR 0011), the sql-runner image uses the
same code.

Only what orch-job and its tools call: S3 (objects, listing, multipart upload), Secrets Manager
``GetSecretValue``, STS ``AssumeRole`` / ``GetCallerIdentity`` and the RDS IAM authentication
token. The clients take and return the same shapes as the boto3 clients they replace (keyword
arguments, response dicts, ``ClientError.response["Error"]["Code"]``), so callers and their
tests do not care which one they get.

Requests are signed with Signature Version 4 (HMAC-SHA256); credentials come, in this order,
from:

* the profile ``AWS_PROFILE`` in ``AWS_CONFIG_FILE`` with ``role_arn`` and ``credential_source =
  Ec2InstanceMetadata`` (orch-job writes it on an SSM runner): the role is assumed with the
  instance profile's credentials (IMDSv2) and assumed again before it expires, so jobs longer
  than the one-hour limit of a role chain keep working;
* ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN``;
* the ECS container credentials endpoint (Fargate task role);
* the instance profile (IMDSv2).

Endpoints are the regional ones (``AWS_REGION``); ``AWS_ENDPOINT_URL_<SERVICE>`` overrides one
(as in the AWS SDKs). TLS is verified with the system trust store; redirects are not followed.
Nothing here logs a credential, a token or a secret value.

Command line (the SSM document fetches the runtime zip with it before the zip is there):
``python3 orch_aws.py s3-download <bucket> <key> <file>``.
"""

from __future__ import annotations

import configparser
import datetime as dt
import hashlib
import hmac
import json
import os
import random
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree  # noqa: S405 - responses of AWS endpoints over verified TLS

IMDS = "http://169.254.169.254"
ECS_CREDENTIALS_HOST = "http://169.254.170.2"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
REFRESH_BEFORE_SECONDS = 300
TIMEOUT_SECONDS = 60
CHUNK = 1024 * 1024
UTC = dt.timezone.utc  # noqa: UP017 - the AMI's python3 is 3.9 (UTC alias from 3.11 only)
# Service name in AWS_ENDPOINT_URL_<SERVICE> and the signing name.
SERVICES = {"s3": "S3", "sts": "STS", "secretsmanager": "SECRETS_MANAGER"}
# Retries (as the SDKs' standard mode): network errors, 5xx and these error codes; at most
# RETRY_ATTEMPTS attempts with full-jitter exponential backoff, each attempt signed again.
RETRY_ATTEMPTS = 5
RETRY_BASE_SECONDS = 0.5
RETRY_MAX_SECONDS = 20.0
# Names spliced into an endpoint host (no other host, port, path or userinfo can come in).
BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
REGION_NAME = re.compile(r"[a-z]{2}(-gov|-iso[a-z]*)?-[a-z]+-[0-9]+")
RETRY_CODES = frozenset({"SlowDown", "Throttling", "ThrottlingException", "RequestTimeout", "RequestLimitExceeded"})


class ClientError(Exception):
    """An error response of an AWS API, shaped like botocore's (``response["Error"]["Code"]``)."""

    def __init__(self, operation: str, code: str, message: str, status: int):
        super().__init__(f"{operation}: {code} (HTTP {status}): {message}")
        self.operation = operation
        self.response = {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": status}}


class CredentialError(Exception):
    """No usable credentials (no profile, keys, container endpoint or instance profile)."""


@dataclass(frozen=True)
class Credentials:
    access_key: str
    secret_key: str
    token: str | None = None
    expires: dt.datetime | None = None

    def __repr__(self) -> str:  # never print a key
        return f"Credentials(access_key={self.access_key[:4]}..., expires={self.expires})"


# --- transport --------------------------------------------------------------------------


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: Any  # file-like: read(n)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:  # a redirect is an error
        return None


_AWS_OPENER = urllib.request.build_opener(_NoRedirect)
# Link-local metadata endpoints: never through a proxy.
_LOCAL_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def http_send(method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> Response:
    """One HTTP request. Error statuses are returned, not raised; network errors raise OSError."""
    local = url.startswith((IMDS, ECS_CREDENTIALS_HOST))
    # Plain http only to the link-local metadata endpoints and to this host (local test servers).
    if not url.startswith(("https://", "http://127.0.0.1:", "http://localhost:")) and not local:
        raise ValueError(f"refusing a plain http AWS endpoint: {url.split('?')[0]}")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310 - https or link-local
    opener = _LOCAL_OPENER if local else _AWS_OPENER
    try:
        resp = opener.open(req, timeout=5 if local else TIMEOUT_SECONDS)
    except urllib.error.HTTPError as err:
        return Response(err.code, {k.lower(): v for k, v in err.headers.items()}, err)
    return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp)


# The transport every client uses unless it is given one (tests replace it: no network).
send = http_send
# Waits between retries and the signing clock (tests replace them: no sleeping).
pause = time.sleep


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


if TYPE_CHECKING:  # the AMI's python3 (3.9) cannot evaluate `bytes | None` at run time
    Transport = Callable[[str, str, dict[str, str], bytes | None], Response]


# --- Signature Version 4 ------------------------------------------------------------------


def _quote(value: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(value, safe=safe)


def canonical_query(params: dict[str, str] | list[tuple[str, str]]) -> str:
    items = params.items() if isinstance(params, dict) else params
    return "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted((str(k), str(v)) for k, v in items))


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signature(creds: Credentials, date: str, region: str, service: str, string_to_sign: str) -> str:
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{creds.secret_key}".encode(), date), region), service), "aws4_request")
    return hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()


def sign(
    method: str,
    url: str,
    headers: dict[str, str],
    payload_hash: str,
    creds: Credentials,
    region: str,
    service: str,
    now: dt.datetime | None = None,
) -> dict[str, str]:
    """The request headers with ``Authorization`` (SigV4). ``url`` carries the encoded path and
    the query exactly as sent; the query is canonicalised (sorted, RFC 3986 encoded)."""
    parts = urllib.parse.urlsplit(url)
    stamp = (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    out = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "host")}
    out.update({"Host": parts.netloc, "X-Amz-Date": stamp})
    if creds.token:
        out["X-Amz-Security-Token"] = creds.token
    if service == "s3":
        out["X-Amz-Content-Sha256"] = payload_hash
    canon = sorted((k.lower(), " ".join(str(v).split())) for k, v in out.items())
    signed = ";".join(k for k, _ in canon)
    query = canonical_query(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    request = "\n".join(
        [method, parts.path or "/", query, "".join(f"{k}:{v}\n" for k, v in canon), signed, payload_hash]
    )
    scope = f"{stamp[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(request.encode()).hexdigest()])
    sig = _signature(creds, stamp[:8], region, service, to_sign)
    out["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={creds.access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    )
    return out


def presign(
    host: str, params: dict[str, str], creds: Credentials, region: str, service: str, expires: int, now: dt.datetime
) -> str:
    """``host/?<params>&X-Amz-...&X-Amz-Signature=...``: a presigned GET (query auth)."""
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{stamp[:8]}/{region}/{service}/aws4_request"
    auth = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{creds.access_key}/{scope}",
        "X-Amz-Date": stamp,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    if creds.token:
        auth["X-Amz-Security-Token"] = creds.token
    query = canonical_query({**params, **auth})
    request = "\n".join(["GET", "/", query, f"host:{host}\n", "host", EMPTY_SHA256])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(request.encode()).hexdigest()])
    return f"{host}/?{query}&X-Amz-Signature={_signature(creds, stamp[:8], region, service, to_sign)}"


# --- credentials -------------------------------------------------------------------------


def _parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def imds_credentials(transport: Transport | None = None) -> Credentials:
    """The instance profile's credentials over IMDSv2 (a session token first)."""
    transport = transport or send
    token = transport("PUT", f"{IMDS}/latest/api/token", {"X-aws-ec2-metadata-token-ttl-seconds": "300"}, b"")
    if token.status != 200:
        raise CredentialError(f"IMDSv2 token: HTTP {token.status}")
    header = {"X-aws-ec2-metadata-token": token.body.read().decode()}
    base = f"{IMDS}/latest/meta-data/iam/security-credentials/"
    names = transport("GET", base, header, None)
    if names.status != 200:
        raise CredentialError(f"no instance profile (IMDS HTTP {names.status})")
    role = names.body.read().decode().split()[0]
    got = transport("GET", base + urllib.parse.quote(role), header, None)
    if got.status != 200:
        raise CredentialError(f"instance profile credentials: HTTP {got.status}")
    data = json.loads(got.body.read())
    return Credentials(data["AccessKeyId"], data["SecretAccessKey"], data.get("Token"), _parse_time(data["Expiration"]))


def container_credentials(transport: Transport | None = None) -> Credentials | None:
    """The ECS task role (Fargate): AWS_CONTAINER_CREDENTIALS_RELATIVE_URI / _FULL_URI."""
    relative = os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
    full = os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
    if relative:
        url = ECS_CREDENTIALS_HOST + relative
    elif full and full.startswith(("http://169.254.170.2", "http://169.254.170.23", "http://127.0.0.1")):
        url = full
    else:
        return None
    headers: dict[str, str] = {}
    token_file = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")
    if token_file:
        headers["Authorization"] = Path(token_file).read_text().strip()
    elif os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN"):
        headers["Authorization"] = os.environ["AWS_CONTAINER_AUTHORIZATION_TOKEN"]
    got = (transport or send)("GET", url, headers, None)
    if got.status != 200:
        raise CredentialError(f"container credentials: HTTP {got.status}")
    data = json.loads(got.body.read())
    expires = _parse_time(data["Expiration"]) if data.get("Expiration") else None
    return Credentials(data["AccessKeyId"], data["SecretAccessKey"], data.get("Token"), expires)


def env_credentials() -> Credentials | None:
    key, secret = os.environ.get("AWS_ACCESS_KEY_ID"), os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not (key and secret):
        return None
    return Credentials(key, secret, os.environ.get("AWS_SESSION_TOKEN") or None)


class Provider:
    """Cached credentials of ``fetch``, fetched again ``REFRESH_BEFORE_SECONDS`` before they expire."""

    def __init__(self, fetch: Callable[[], Credentials], clock: Callable[[], dt.datetime] | None = None):
        self._fetch = fetch
        self._clock = clock or (lambda: dt.datetime.now(UTC))
        self._creds: Credentials | None = None
        self._lock = threading.Lock()

    def __call__(self) -> Credentials:
        with self._lock:
            c = self._creds
            if c is None or (c.expires and (c.expires - self._clock()).total_seconds() < REFRESH_BEFORE_SECONDS):
                self._creds = c = self._fetch()
            return c


def assume_role(
    source: Callable[[], Credentials],
    role_arn: str,
    session_name: str,
    region: str,
    duration: int = 3600,
    transport: Transport | None = None,
) -> Credentials:
    """STS AssumeRole signed with the source credentials."""
    body = urllib.parse.urlencode(
        {
            "Action": "AssumeRole",
            "Version": "2011-06-15",
            "RoleArn": role_arn,
            "RoleSessionName": session_name,
            "DurationSeconds": str(duration),
        }
    ).encode()
    root = _call_query(Client("sts", region, source, transport), "AssumeRole", body)
    c = _find(root, "AssumeRoleResult/Credentials")
    return Credentials(
        _text(c, "AccessKeyId"),
        _text(c, "SecretAccessKey"),
        _text(c, "SessionToken"),
        _parse_time(_text(c, "Expiration")),
    )


def profile_role(profile: str | None = None, config_file: str | None = None) -> dict[str, str] | None:
    """``role_arn`` / ``role_session_name`` / ``duration_seconds`` of an assume-role profile whose
    source is the instance profile, or None."""
    profile = profile or os.environ.get("AWS_PROFILE")
    path = config_file or os.environ.get("AWS_CONFIG_FILE")
    if not (profile and path and Path(path).is_file()):
        return None
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path)
    section = profile if profile == "default" else f"profile {profile}"
    if not cfg.has_section(section) or not cfg.get(section, "role_arn", fallback=""):
        return None
    source = cfg.get(section, "credential_source", fallback="")
    if source != "Ec2InstanceMetadata":
        raise CredentialError(f"profile {profile}: credential_source {source!r} is not supported")
    return {
        "role_arn": cfg.get(section, "role_arn"),
        "role_session_name": cfg.get(section, "role_session_name", fallback="orch-job"),
        "duration_seconds": cfg.get(section, "duration_seconds", fallback="3600"),
    }


def default_provider(
    region: str, transport: Transport | None = None, profile: str | None = None
) -> Callable[[], Credentials]:
    role = profile_role(profile)
    if role:
        instance = Provider(lambda: imds_credentials(transport))
        return Provider(
            lambda: assume_role(
                instance,
                role["role_arn"],
                role["role_session_name"],
                region,
                int(role["duration_seconds"]),
                transport,
            )
        )
    static = env_credentials()
    if static:
        return lambda: static
    if os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI") or os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI"):
        return Provider(lambda: _required(container_credentials(transport)))
    return Provider(lambda: imds_credentials(transport))


def _required(creds: Credentials | None) -> Credentials:
    if creds is None:
        raise CredentialError("no container credentials")
    return creds


def region_name() -> str:
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise CredentialError("AWS_REGION is not set")
    return region


# --- clients -----------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(root: ElementTree.Element, path: str) -> ElementTree.Element:
    node: ElementTree.Element | None = root
    for name in path.split("/"):
        node = next((c for c in node if _local(c.tag) == name), None) if node is not None else None
    if node is None:
        raise ValueError(f"unexpected AWS response: no {path}")
    return node


def _text(root: ElementTree.Element, path: str, default: str | None = None) -> str:
    try:
        return _find(root, path).text or ""
    except ValueError:
        if default is not None:
            return default
        raise


def _xml(data: bytes) -> ElementTree.Element:
    return ElementTree.fromstring(data)  # noqa: S314 - AWS endpoint over verified TLS


def _error(operation: str, resp: Response) -> ClientError:
    data = resp.body.read() if resp.body is not None else b""
    code, message = str(resp.status), ""
    try:
        if data.lstrip().startswith(b"<"):
            root = _xml(data)
            err = root if _local(root.tag) == "Error" else _find(root, "Error")
            code, message = _text(err, "Code", code), _text(err, "Message", "")
        elif data:
            doc = json.loads(data)
            code = str(doc.get("__type") or doc.get("code") or code).rsplit("#", 1)[-1]
            message = str(doc.get("message") or doc.get("Message") or "")
    except (ValueError, ElementTree.ParseError):
        pass
    if resp.status == 404 and code == "404":
        code = "NoSuchKey" if operation in ("GetObject", "HeadObject") else "NotFound"
    return ClientError(operation, code, message[:500], resp.status)


class Client:
    """One service in one region; ``credentials`` is a provider (called for each request)."""

    def __init__(
        self, service: str, region: str, credentials: Callable[[], Credentials], transport: Transport | None = None
    ):
        if not REGION_NAME.fullmatch(region):
            raise ValueError(f"not an AWS region name: {region!r}")
        self.service, self.region, self._credentials, self._transport = service, region, credentials, transport
        self.endpoint = os.environ.get(f"AWS_ENDPOINT_URL_{SERVICES.get(service, service.upper())}", "").rstrip("/")
        suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
        self.default_host = f"{service}.{region}.{suffix}"

    def request(
        self,
        operation: str,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        error_in_200: bool = False,
    ) -> Response:
        """The response of a 2xx; retries transient failures (``RETRY_CODES``, 5xx, network
        errors). Only a ``bytes`` body is sent again: anything else gets one attempt.
        ``error_in_200``: a 200 whose body is an ``<Error>`` document is an error (S3
        CompleteMultipartUpload); the body is then read and returned as bytes."""
        payload = hashlib.sha256(body).hexdigest()
        attempts = RETRY_ATTEMPTS if isinstance(body, bytes) else 1
        for attempt in range(1, attempts + 1):
            last = attempt == attempts
            # Signed again each attempt: fresh X-Amz-Date, fresh credentials if they rotated.
            signed = sign(method, url, headers or {}, payload, self._credentials(), self.region, self.service)
            try:
                resp = (self._transport or send)(method, url, signed, body if method in ("PUT", "POST") else None)
            except OSError as err:
                if last or not _transient(err):
                    raise
                pause(_backoff(attempt))
                continue
            if error_in_200 and resp.status == 200:
                data = resp.body.read()
                errored = data.lstrip().startswith(b"<") and _local(_xml(data).tag) == "Error"
                resp = Response(500 if errored else 200, resp.headers, _Bytes(data))
            if resp.status < 300:
                return resp
            err = _error(operation, resp)
            if last or not (resp.status >= 500 or err.response["Error"]["Code"] in RETRY_CODES):
                raise err
            pause(_backoff(attempt))
        raise AssertionError("unreachable")


def _transient(err: OSError) -> bool:
    """A network error worth another attempt; a failed certificate check is not one."""
    reason = getattr(err, "reason", err)
    return not isinstance(reason, ssl.SSLCertVerificationError) and not isinstance(err, ssl.SSLCertVerificationError)


def _backoff(attempt: int) -> float:
    """Full jitter: uniform in [0, base * 2^(attempt-1)], capped."""
    return random.uniform(0, min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** (attempt - 1)))  # noqa: S311


def _call_query(client: Client, operation: str, body: bytes) -> ElementTree.Element:
    url = (client.endpoint or f"https://{client.default_host}") + "/"
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
    return _xml(client.request(operation, "POST", url, headers, body).body.read())


class StreamingBody:
    """``read(n)`` / ``iter_chunks`` over an HTTP response, like botocore's."""

    def __init__(self, raw: Any):
        self._raw = raw

    def read(self, amt: int | None = None) -> bytes:
        return self._raw.read() if amt is None else self._raw.read(amt)

    def iter_chunks(self, chunk_size: int = CHUNK) -> Iterator[bytes]:
        yield from iter(lambda: self._raw.read(chunk_size), b"")

    def close(self) -> None:
        self._raw.close()


class _Paginator:
    def __init__(self, s3: S3):
        self._s3 = s3

    def paginate(self, Bucket: str, Prefix: str = "") -> Iterator[dict[str, Any]]:  # noqa: N803 - boto3 names
        token: str | None = None
        while True:
            page = self._s3.list_objects_v2(
                Bucket=Bucket, Prefix=Prefix, **({"ContinuationToken": token} if token else {})
            )
            yield page
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or not token:
                return


_SSE = {
    "ServerSideEncryption": "x-amz-server-side-encryption",
    "SSEKMSKeyId": "x-amz-server-side-encryption-aws-kms-key-id",
}


class S3(Client):
    """The S3 calls of the runtime (boto3 names and shapes)."""

    def __init__(self, region: str, credentials: Callable[[], Credentials], transport: Transport | None = None):
        super().__init__("s3", region, credentials, transport)

    def _url(self, bucket: str, key: str = "", query: dict[str, str] | None = None) -> str:
        if not BUCKET_NAME.fullmatch(bucket):
            raise ValueError(f"not an S3 bucket name: {bucket!r}")
        path = "/" + _quote(key, safe="-_.~/") if key else "/"
        if self.endpoint:
            base = f"{self.endpoint}/{bucket}"
        elif "." in bucket:
            base = f"https://{self.default_host}/{bucket}"  # path style: dotted names break the TLS name
        else:
            base = f"https://{bucket}.{self.default_host}"
        return base + path + (f"?{canonical_query(query)}" if query else "")

    @staticmethod
    def _headers(kw: dict[str, Any]) -> dict[str, str]:
        headers = {header: str(kw[name]) for name, header in _SSE.items() if kw.get(name)}
        if kw.get("ContentType"):
            headers["Content-Type"] = str(kw["ContentType"])
        return headers

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        resp = self.request("GetObject", "GET", self._url(Bucket, Key))
        return {"Body": StreamingBody(resp.body), "ContentLength": int(resp.headers.get("content-length") or 0)}

    def put_object(self, Bucket: str, Key: str, Body: bytes, **kw: Any) -> dict[str, Any]:  # noqa: N803
        resp = self.request("PutObject", "PUT", self._url(Bucket, Key), self._headers(kw), bytes(Body))
        resp.body.read()
        return {"ETag": resp.headers.get("etag", "")}

    def list_objects_v2(self, Bucket: str, Prefix: str = "", ContinuationToken: str = "") -> dict[str, Any]:  # noqa: N803
        query = {"list-type": "2", "prefix": Prefix}
        if ContinuationToken:
            query["continuation-token"] = ContinuationToken
        root = _xml(self.request("ListObjectsV2", "GET", self._url(Bucket, query=query)).body.read())
        contents = [
            {"Key": _text(c, "Key"), "Size": int(_text(c, "Size", "0"))} for c in root if _local(c.tag) == "Contents"
        ]
        page: dict[str, Any] = {"IsTruncated": _text(root, "IsTruncated", "false") == "true"}
        if contents:
            page["Contents"] = contents
        if _text(root, "NextContinuationToken", ""):
            page["NextContinuationToken"] = _text(root, "NextContinuationToken")
        return page

    def get_paginator(self, operation: str) -> _Paginator:
        if operation != "list_objects_v2":
            raise ValueError(f"no paginator for {operation}")
        return _Paginator(self)

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:  # noqa: N803
        body = self.get_object(Bucket=Bucket, Key=Key)["Body"]
        with open(Filename, "wb") as fh:
            for chunk in body.iter_chunks():
                fh.write(chunk)

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None:  # noqa: N803
        # Artifacts of a job: small files (dumps go through the multipart calls of pg-dump-s3).
        self.put_object(Bucket=Bucket, Key=Key, Body=Path(Filename).read_bytes())

    def create_multipart_upload(self, Bucket: str, Key: str, **kw: Any) -> dict[str, Any]:  # noqa: N803
        resp = self.request("CreateMultipartUpload", "POST", self._url(Bucket, Key) + "?uploads", self._headers(kw))
        return {"UploadId": _text(_xml(resp.body.read()), "UploadId")}

    def upload_part(self, Bucket: str, Key: str, UploadId: str, PartNumber: int, Body: bytes) -> dict[str, Any]:  # noqa: N803
        url = self._url(Bucket, Key, {"partNumber": str(PartNumber), "uploadId": UploadId})
        resp = self.request("UploadPart", "PUT", url, {}, bytes(Body))
        resp.body.read()
        return {"ETag": resp.headers.get("etag", "")}

    def complete_multipart_upload(
        self,
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
        UploadId: str,  # noqa: N803
        MultipartUpload: dict[str, Any],  # noqa: N803
    ) -> dict[str, Any]:
        parts = "".join(
            f"<Part><PartNumber>{int(p['PartNumber'])}</PartNumber><ETag>{_escape(p['ETag'])}</ETag></Part>"
            for p in MultipartUpload["Parts"]
        )
        body = f"<CompleteMultipartUpload>{parts}</CompleteMultipartUpload>".encode()
        # S3 can answer 200 with an error document once the upload has started (error_in_200).
        url = self._url(Bucket, Key, {"uploadId": UploadId})
        self.request("CompleteMultipartUpload", "POST", url, {}, body, error_in_200=True).body.read()
        return {}

    def abort_multipart_upload(self, Bucket: str, Key: str, UploadId: str) -> dict[str, Any]:  # noqa: N803
        self.request("AbortMultipartUpload", "DELETE", self._url(Bucket, Key, {"uploadId": UploadId})).body.read()
        return {}


class _Bytes:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, *_a: Any) -> bytes:
        data, self._data = self._data, b""
        return data


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


class SecretsManager(Client):
    def __init__(self, region: str, credentials: Callable[[], Credentials], transport: Transport | None = None):
        super().__init__("secretsmanager", region, credentials, transport)

    def get_secret_value(self, SecretId: str) -> dict[str, Any]:  # noqa: N803
        url = (self.endpoint or f"https://{self.default_host}") + "/"
        headers = {"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": "secretsmanager.GetSecretValue"}
        resp = self.request("GetSecretValue", "POST", url, headers, json.dumps({"SecretId": SecretId}).encode())
        return json.loads(resp.body.read())


class Sts(Client):
    def __init__(self, region: str, credentials: Callable[[], Credentials], transport: Transport | None = None):
        super().__init__("sts", region, credentials, transport)

    def get_caller_identity(self) -> dict[str, str]:
        body = urllib.parse.urlencode({"Action": "GetCallerIdentity", "Version": "2011-06-15"}).encode()
        result = _find(_call_query(self, "GetCallerIdentity", body), "GetCallerIdentityResult")
        return {"Arn": _text(result, "Arn"), "Account": _text(result, "Account"), "UserId": _text(result, "UserId")}


class Rds:
    """Only the IAM database authentication token: a local signature, no request."""

    def __init__(self, region: str, credentials: Callable[[], Credentials]):
        self.region, self._credentials = region, credentials

    def generate_db_auth_token(
        self,
        DBHostname: str,  # noqa: N803
        Port: int,  # noqa: N803
        DBUsername: str,  # noqa: N803
        Region: str | None = None,  # noqa: N803
        now: dt.datetime | None = None,
    ) -> str:
        return presign(
            f"{DBHostname}:{int(Port)}",
            {"Action": "connect", "DBUser": DBUsername},
            self._credentials(),
            Region or self.region,
            "rds-db",
            900,
            now or dt.datetime.now(UTC),
        )


class Session:
    """``boto3.Session`` stand-in: clients sharing one credential provider."""

    def __init__(
        self, profile_name: str | None = None, region_name_: str | None = None, transport: Transport | None = None
    ):
        self.region = region_name_ or region_name()
        self._transport = transport
        self._credentials = default_provider(self.region, transport, profile_name)

    def client(self, name: str, region_name: str | None = None) -> Any:
        region = region_name or self.region
        if name == "s3":
            return S3(region, self._credentials, self._transport)
        if name == "secretsmanager":
            return SecretsManager(region, self._credentials, self._transport)
        if name == "sts":
            return Sts(region, self._credentials, self._transport)
        if name == "rds":
            return Rds(region, self._credentials)
        raise ValueError(f"no stdlib client for {name}")


def client(name: str, region_name: str | None = None) -> Any:
    """A client with the default credentials (``boto3.client`` stand-in)."""
    return Session(region_name_=region_name).client(name)


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[0] != "s3-download":
        print("usage: orch_aws.py s3-download <bucket> <key> <file>", file=sys.stderr)
        return 2
    try:
        client("s3").download_file(Bucket=argv[1], Key=argv[2], Filename=argv[3])
    except (ClientError, CredentialError, OSError, ValueError) as err:
        print(f"orch_aws: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
