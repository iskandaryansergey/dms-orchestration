"""orch_aws: the job runtime's AWS client on the Python standard library (the runner AMI has no
boto3). Signatures are checked against botocore and a published SigV4 test vector; every other
call runs against a fake transport, one against a local HTTP server. No AWS, no network."""

from __future__ import annotations

import datetime as dt
import http.server
import json
import re
import subprocess
import sys
import threading
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orch_aws
import pytest

NOW = dt.datetime(2026, 11, 2, 10, 30, 0, tzinfo=dt.timezone.utc)  # noqa: UP017 - same as the runtime
CREDS = orch_aws.Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "session-token-1")
ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "modules" / "dms-orchestration" / "src" / "jobs" / "sql-runner"
SRC = ROOT / "modules" / "dms-orchestration" / "src"


class Body:
    def __init__(self, data: bytes | str):
        self._data = data.encode() if isinstance(data, str) else data

    def read(self, n: int | None = None) -> bytes:
        if n is None:
            data, self._data = self._data, b""
        else:
            data, self._data = self._data[:n], self._data[n:]
        return data

    def close(self) -> None:
        pass


class FakeTransport:
    """Answers by (method, url prefix); records every request."""

    def __init__(self, routes: list[tuple[str, str, Any]]):
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> orch_aws.Response:
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        for m, prefix, answer in self.routes:
            if m == method and url.startswith(prefix):
                status, data, resp_headers = answer(url, headers, body) if callable(answer) else answer
                return orch_aws.Response(status, resp_headers, Body(data))
        raise AssertionError(f"unexpected request {method} {url}")


# --- Signature Version 4 -------------------------------------------------------------------


def test_sigv4_matches_the_published_get_vanilla_vector():
    creds = orch_aws.Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    now = dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.timezone.utc)  # noqa: UP017
    headers = orch_aws.sign(
        "GET", "https://example.amazonaws.com/", {}, orch_aws.EMPTY_SHA256, creds, "us-east-1", "service", now
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def _botocore_auth(method: str, url: str, headers: dict[str, str], body: bytes, service: str, monkeypatch) -> str:
    import botocore.auth
    import botocore.credentials
    from botocore.awsrequest import AWSRequest

    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda: NOW.replace(tzinfo=None))
    request = AWSRequest(method=method, url=url, headers=dict(headers), data=body)
    creds = botocore.credentials.Credentials(CREDS.access_key, CREDS.secret_key, CREDS.token)
    signer = botocore.auth.S3SigV4Auth if service == "s3" else botocore.auth.SigV4Auth
    signer(creds, service, "eu-west-2").add_auth(request)
    return request.headers["Authorization"]


@pytest.mark.parametrize(
    "method,url,headers,body,service",
    [
        # http: botocore signs the S3 payload hash only without TLS; the canonical form is the same.
        ("GET", "http://b.s3.eu-west-2.amazonaws.com/jobs/a%20b/%C3%BC%2B%3D.sql", {}, b"", "s3"),
        (
            "PUT",
            "http://b.s3.eu-west-2.amazonaws.com/dumps/x/target.dump?partNumber=2&uploadId=u%2F1",
            {"x-amz-server-side-encryption": "aws:kms", "Content-Type": "application/octet-stream"},
            b"\x00part",
            "s3",
        ),
        ("GET", "http://b.s3.eu-west-2.amazonaws.com/?list-type=2&prefix=jobs%2Fledger%2F", {}, b"", "s3"),
        (
            "POST",
            "https://sts.eu-west-2.amazonaws.com/",
            {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
            b"Action=GetCallerIdentity&Version=2011-06-15",
            "sts",
        ),
        (
            "POST",
            "https://secretsmanager.eu-west-2.amazonaws.com/",
            {"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": "secretsmanager.GetSecretValue"},
            b'{"SecretId": "arn:aws:secretsmanager:eu-west-2:111122223333:secret:x"}',
            "secretsmanager",
        ),
    ],
)
def test_sigv4_matches_botocore(method, url, headers, body, service, monkeypatch):
    import hashlib

    ours = orch_aws.sign(method, url, headers, hashlib.sha256(body).hexdigest(), CREDS, "eu-west-2", service, NOW)[
        "Authorization"
    ]
    assert ours == _botocore_auth(method, url, headers, body, service, monkeypatch)


def test_rds_auth_token_matches_boto3(monkeypatch):
    import boto3
    import botocore.auth

    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda: NOW.replace(tzinfo=None))
    expected = boto3.client(
        "rds",
        region_name="eu-west-2",
        aws_access_key_id=CREDS.access_key,
        aws_secret_access_key=CREDS.secret_key,
        aws_session_token=CREDS.token,
    ).generate_db_auth_token(DBHostname="db.cluster-x.eu-west-2.rds.amazonaws.com", Port=5432, DBUsername="orch_iam")
    ours = orch_aws.Rds("eu-west-2", lambda: CREDS).generate_db_auth_token(
        DBHostname="db.cluster-x.eu-west-2.rds.amazonaws.com", Port=5432, DBUsername="orch_iam", now=NOW
    )

    def parts(token: str) -> tuple[str, dict[str, list[str]]]:
        host, _, query = token.partition("/?")
        return host, urllib.parse.parse_qs(query)

    assert parts(ours) == parts(expected)


# --- credentials ---------------------------------------------------------------------------

IMDS_CREDS = {
    "AccessKeyId": "ASIAINSTANCE",
    "SecretAccessKey": "instance-secret",
    "Token": "instance-token",
    "Expiration": "2026-11-02T16:30:00Z",
}


def imds_routes(creds: dict[str, str] | None = None) -> list[tuple[str, str, Any]]:
    def token(_url: str, headers: dict[str, str], _body: Any) -> Any:
        assert headers["X-aws-ec2-metadata-token-ttl-seconds"] == "300"
        return 200, "imds-session", {}

    def need_token(answer: Any) -> Any:
        def handler(_url: str, headers: dict[str, str], _body: Any) -> Any:
            assert headers.get("X-aws-ec2-metadata-token") == "imds-session", "IMDSv2 only"
            return answer

        return handler

    base = f"{orch_aws.IMDS}/latest/meta-data/iam/security-credentials/"
    return [
        ("PUT", f"{orch_aws.IMDS}/latest/api/token", token),
        ("GET", base + "runner-role", need_token((200, json.dumps(creds or IMDS_CREDS), {}))),
        ("GET", base, need_token((200, "runner-role\n", {}))),
    ]


def test_instance_profile_credentials_come_over_imdsv2():
    t = FakeTransport(imds_routes())
    creds = orch_aws.imds_credentials(t)
    assert (creds.access_key, creds.token) == ("ASIAINSTANCE", "instance-token")
    assert creds.expires == dt.datetime(2026, 11, 2, 16, 30, tzinfo=dt.timezone.utc)  # noqa: UP017
    assert [c["method"] for c in t.calls] == ["PUT", "GET", "GET"]
    assert "instance-secret" not in repr(creds)


def assume_role_answer(key: str, expiration: str) -> Any:
    def answer(_url: str, headers: dict[str, str], body: bytes) -> Any:
        form = urllib.parse.parse_qs(body.decode())
        assert form["Action"] == ["AssumeRole"] and form["DurationSeconds"] == ["3600"]
        xml = f"""<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleResult>
<Credentials><AccessKeyId>{key}</AccessKeyId><SecretAccessKey>job-secret</SecretAccessKey>
<SessionToken>job-token</SessionToken><Expiration>{expiration}</Expiration></Credentials>
</AssumeRoleResult></AssumeRoleResponse>"""
        return 200, xml, {}

    return answer


def write_profile(tmp_path: Path, monkeypatch, role: str = "arn:aws:iam::111122223333:role/dms-orch-test-job-ledger"):
    config = tmp_path / "aws-config"
    config.write_text(
        f"[profile orch-job]\nrole_arn = {role}\nrole_session_name = orch-ledger-run-1\n"
        "credential_source = Ec2InstanceMetadata\nduration_seconds = 3600\n"
    )
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_PROFILE", "orch-job")
    monkeypatch.setenv("AWS_REGION", "eu-west-2")


def test_the_job_profile_assumes_the_job_role_with_the_instance_profile(tmp_path, monkeypatch):
    write_profile(tmp_path, monkeypatch)
    sts = assume_role_answer("ASIAJOB", "2026-11-02T11:30:00Z")
    t = FakeTransport(
        [
            *imds_routes(),
            ("POST", "https://sts.eu-west-2.amazonaws.com/", sts),
            ("GET", "https://bucket-1.s3.eu-west-2.amazonaws.com/runs/ledger/spec.json", (200, b"{}", {})),
        ]
    )
    s3 = orch_aws.Session(profile_name="orch-job", transport=t).client("s3")
    assert s3.get_object(Bucket="bucket-1", Key="runs/ledger/spec.json")["Body"].read() == b"{}"
    assume, get = t.calls[3], t.calls[4]
    # AssumeRole is signed with the instance profile, regional STS; everything else with the job role.
    assert "Credential=ASIAINSTANCE/" in assume["headers"]["Authorization"]
    assert assume["headers"]["X-Amz-Security-Token"] == "instance-token"
    form = urllib.parse.parse_qs(assume["body"].decode())
    assert form["RoleArn"] == ["arn:aws:iam::111122223333:role/dms-orch-test-job-ledger"]
    assert form["RoleSessionName"] == ["orch-ledger-run-1"]
    assert "Credential=ASIAJOB/" in get["headers"]["Authorization"]
    assert get["headers"]["X-Amz-Security-Token"] == "job-token"


def test_the_job_role_is_assumed_again_before_it_expires():
    clock = [dt.datetime(2026, 11, 2, 10, 0, tzinfo=dt.timezone.utc)]  # noqa: UP017
    issued: list[int] = []

    def fetch() -> orch_aws.Credentials:
        issued.append(1)
        return orch_aws.Credentials(f"K{len(issued)}", "s", "t", clock[0] + dt.timedelta(hours=1))

    provider = orch_aws.Provider(fetch, clock=lambda: clock[0])
    assert provider().access_key == "K1"
    clock[0] += dt.timedelta(minutes=50)
    assert provider().access_key == "K1"
    clock[0] += dt.timedelta(minutes=6)  # less than 5 minutes left
    assert provider().access_key == "K2"


def test_a_profile_with_another_credential_source_is_refused(tmp_path, monkeypatch):
    write_profile(tmp_path, monkeypatch)
    config = Path(str(tmp_path / "aws-config"))
    config.write_text(config.read_text().replace("Ec2InstanceMetadata", "Environment"))
    with pytest.raises(orch_aws.CredentialError):
        orch_aws.Session(profile_name="orch-job", transport=FakeTransport([]))


def test_fargate_uses_the_container_credentials(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE", "AWS_CONFIG_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", "eu-west-2")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/credentials/abc")
    task = {"AccessKeyId": "ASIATASK", "SecretAccessKey": "s", "Token": "t", "Expiration": "2026-11-02T16:30:00Z"}
    t = FakeTransport(
        [
            ("GET", "http://169.254.170.2/v2/credentials/abc", (200, json.dumps(task), {})),
            ("POST", "https://secretsmanager.eu-west-2.amazonaws.com/", (200, json.dumps({"SecretString": "{}"}), {})),
        ]
    )
    sm = orch_aws.Session(transport=t).client("secretsmanager")
    assert sm.get_secret_value(SecretId="arn:x")["SecretString"] == "{}"
    assert "Credential=ASIATASK/" in t.calls[1]["headers"]["Authorization"]
    assert t.calls[1]["headers"]["X-Amz-Target"] == "secretsmanager.GetSecretValue"


# --- clients -------------------------------------------------------------------------------


def s3_client(t: FakeTransport) -> orch_aws.S3:
    return orch_aws.S3("eu-west-2", lambda: CREDS, t)


def test_s3_urls_virtual_hosted_path_style_for_dotted_buckets_and_the_endpoint_override(monkeypatch):
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    s3 = s3_client(FakeTransport([]))
    assert s3._url("bkt-1", "a b/x.sql") == "https://bkt-1.s3.eu-west-2.amazonaws.com/a%20b/x.sql"
    assert s3._url("bkt.1", "k") == "https://s3.eu-west-2.amazonaws.com/bkt.1/k"
    assert orch_aws.S3("cn-north-1", lambda: CREDS)._url("bkt", "k") == "https://bkt.s3.cn-north-1.amazonaws.com.cn/k"
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://bucket.vpce-1.s3.eu-west-2.vpce.amazonaws.com/")
    assert (
        s3_client(FakeTransport([]))._url("bkt", "k") == "https://bucket.vpce-1.s3.eu-west-2.vpce.amazonaws.com/bkt/k"
    )


def test_s3_list_pages_until_the_listing_is_complete():
    def page(url: str, *_a: Any) -> Any:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if "continuation-token" not in q:
            return (
                200,
                "<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'><IsTruncated>true</IsTruncated>"
                "<Contents><Key>jobs/a.sql</Key><Size>3</Size></Contents>"
                "<NextContinuationToken>t/2</NextContinuationToken></ListBucketResult>",
                {},
            )
        assert q["continuation-token"] == ["t/2"]
        return (
            200,
            "<ListBucketResult><IsTruncated>false</IsTruncated>"
            "<Contents><Key>jobs/b.sql</Key><Size>4</Size></Contents></ListBucketResult>",
            {},
        )

    s3 = s3_client(FakeTransport([("GET", "https://bkt.s3.eu-west-2.amazonaws.com/?", page)]))
    pages = list(s3.get_paginator("list_objects_v2").paginate(Bucket="bkt", Prefix="jobs/"))
    assert [o["Key"] for p in pages for o in p["Contents"]] == ["jobs/a.sql", "jobs/b.sql"]


def test_s3_errors_carry_the_botocore_shape(pauses):
    missing = (404, "<Error><Code>NoSuchKey</Code><Message>gone</Message></Error>", {})
    s3 = s3_client(FakeTransport([("GET", "https://bkt.s3", missing)]))
    with pytest.raises(orch_aws.ClientError) as err:
        s3.get_object(Bucket="bkt", Key="k")
    assert err.value.response["Error"]["Code"] == "NoSuchKey"
    assert err.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404


def test_s3_multipart_upload_with_kms():
    complete: list[bytes] = []

    def finish(_url: str, _h: Any, body: bytes) -> Any:
        complete.append(body)
        return 200, "<CompleteMultipartUploadResult><ETag>x</ETag></CompleteMultipartUploadResult>", {}

    t = FakeTransport(
        [
            (
                "POST",
                "https://bkt.s3.eu-west-2.amazonaws.com/d/t.dump?uploads",
                (200, "<R><UploadId>u1</UploadId></R>", {}),
            ),
            (
                "PUT",
                "https://bkt.s3.eu-west-2.amazonaws.com/d/t.dump?partNumber=1&uploadId=u1",
                (200, "", {"etag": '"e1"'}),
            ),
            ("POST", "https://bkt.s3.eu-west-2.amazonaws.com/d/t.dump?uploadId=u1", finish),
        ]
    )
    s3 = s3_client(t)
    up = s3.create_multipart_upload(Bucket="bkt", Key="d/t.dump", ServerSideEncryption="aws:kms", SSEKMSKeyId="k-1")
    etag = s3.upload_part(Bucket="bkt", Key="d/t.dump", UploadId=up["UploadId"], PartNumber=1, Body=b"data")["ETag"]
    s3.complete_multipart_upload(
        Bucket="bkt", Key="d/t.dump", UploadId="u1", MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": etag}]}
    )
    assert t.calls[0]["headers"]["x-amz-server-side-encryption"] == "aws:kms"
    assert t.calls[0]["headers"]["x-amz-server-side-encryption-aws-kms-key-id"] == "k-1"
    assert t.calls[1]["body"] == b"data"
    assert complete == [
        b"<CompleteMultipartUpload><Part><PartNumber>1</PartNumber><ETag>&quot;e1&quot;</ETag></Part></CompleteMultipartUpload>"
    ]


def test_s3_complete_multipart_upload_error_inside_a_200_is_an_error(pauses):
    err_doc = (200, "<Error><Code>InternalError</Code><Message>retry</Message></Error>", {})
    s3 = s3_client(FakeTransport([("POST", "https://bkt.s3", err_doc)]))
    with pytest.raises(orch_aws.ClientError, match="InternalError"):
        s3.complete_multipart_upload(Bucket="bkt", Key="k", UploadId="u", MultipartUpload={"Parts": []})


def test_plain_http_is_refused_except_for_metadata_and_this_host():
    with pytest.raises(ValueError, match="plain http"):
        orch_aws.http_send("GET", "http://s3.eu-west-2.amazonaws.com/b/k", {})


# --- over a real socket: the bootstrap download of the SSM document --------------------------


@pytest.fixture
def local_s3(monkeypatch) -> Iterator[dict[str, Any]]:
    seen: dict[str, Any] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            seen["path"], seen["auth"] = self.path, self.headers.get("Authorization", "")
            if self.path == "/moved/k":
                self.send_response(307)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.end_headers()
                return
            data = b"runtime-zip-bytes"
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_a: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(orch_aws, "send", orch_aws.http_send)  # this test only: a local server
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("AWS_REGION", "eu-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDLOCAL")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "local")
    for name in ("AWS_PROFILE", "AWS_CONFIG_FILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    yield seen
    server.shutdown()


def test_s3_download_command_writes_the_object(local_s3, tmp_path):
    dest = tmp_path / "runtime.zip"
    assert orch_aws.main(["s3-download", "artifacts", "runtime/abc/sql-runner.zip", str(dest)]) == 0
    assert dest.read_bytes() == b"runtime-zip-bytes"
    assert local_s3["path"] == "/artifacts/runtime/abc/sql-runner.zip"
    assert local_s3["auth"].startswith("AWS4-HMAC-SHA256 Credential=AKIDLOCAL/")


def test_redirects_are_not_followed(local_s3, tmp_path, capsys):
    assert orch_aws.main(["s3-download", "moved", "k", str(tmp_path / "x")]) == 1
    assert "307" in capsys.readouterr().err


def test_every_content_type_on_the_wire_is_signed(monkeypatch):
    """urllib adds an unsigned ``Content-Type: application/x-www-form-urlencoded`` to a request
    with a body unless one is set; S3 then rejects the signature of CompleteMultipartUpload."""
    seen: list[dict[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self, data: bytes, headers: dict[str, str] | None = None) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append({k.lower(): v for k, v in self.headers.items()})
            self.send_response(200)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if self.path.endswith("?uploads"):
                self._answer(b"<R><UploadId>u1</UploadId></R>")
            else:
                self._answer(b"<CompleteMultipartUploadResult><ETag>x</ETag></CompleteMultipartUploadResult>")

        def do_PUT(self) -> None:  # noqa: N802 - http.server API
            self._answer(b"", {"ETag": '"e1"'})

        def log_message(self, *_a: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(orch_aws, "send", orch_aws.http_send)  # this test only: a local server
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        s3 = orch_aws.S3("eu-west-2", lambda: CREDS)
        up = s3.create_multipart_upload(Bucket="bkt", Key="d/t.dump")
        etag = s3.upload_part(Bucket="bkt", Key="d/t.dump", UploadId=up["UploadId"], PartNumber=1, Body=b"data")
        s3.complete_multipart_upload(
            Bucket="bkt", Key="d/t.dump", UploadId="u1", MultipartUpload={"Parts": [{"PartNumber": 1, **etag}]}
        )
    finally:
        server.shutdown()
    assert len(seen) == 3
    for headers in seen:
        signed = re.search(r"SignedHeaders=([^,]+)", headers["authorization"])
        assert signed and "content-type" in signed.group(1).split(";"), headers.get("content-type")
    assert seen[2]["content-type"] == "application/xml"


# --- the runner runtime needs no boto3 ------------------------------------------------------


def test_the_runtime_imports_without_boto3():
    code = (
        "import sys\n"
        "sys.modules['boto3'] = None\n"
        "sys.modules['botocore'] = None\n"
        f"sys.path[:0] = [{str(RUNTIME)!r}, {str(SRC)!r}]\n"
        "import orch_aws, orch_job, orch_sql, orch_dump, orch_verify\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_orch_aws_can_be_embedded_in_the_ssm_document():
    text = (RUNTIME / "orch_aws.py").read_text(encoding="utf-8")
    assert "{{" not in text  # SSM would read it as a document parameter
    assert "\nORCH_AWS_PY\n" not in text  # the here-document delimiter of orch-job.sh
    script = (ROOT / "modules" / "dms-orchestration" / "ssm" / "orch-job.sh.tftpl").read_text(encoding="utf-8")
    assert not re.search(r"import boto3|boto3\.", script) and "${orch_aws_py}" in script


# --- retries (boto3 retried; the stdlib client must too) -------------------------------------


@pytest.fixture
def pauses(monkeypatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(orch_aws, "pause", slept.append)
    return slept


class Flaky:
    """Fails with each of ``failures`` in turn (an exception is raised, a tuple is answered), then 200."""

    def __init__(self, failures: list[Any], ok: tuple[int, str, dict[str, str]] = (200, "{}", {})):
        self.failures, self.ok = list(failures), ok
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> orch_aws.Response:
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        answer = self.failures.pop(0) if self.failures else self.ok
        if isinstance(answer, BaseException):
            raise answer
        status, data, resp_headers = answer
        return orch_aws.Response(status, resp_headers, Body(data))


def xml_error(status: int, code: str) -> tuple[int, str, dict[str, str]]:
    return status, f"<Error><Code>{code}</Code><Message>m</Message></Error>", {}


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionResetError(104, "Connection reset by peer"),
        TimeoutError("timed out"),
        orch_aws.urllib.error.URLError(ConnectionRefusedError(111, "refused")),
        xml_error(500, "InternalError"),
        xml_error(502, "502"),
        xml_error(503, "SlowDown"),
        xml_error(503, "ServiceUnavailable"),
        xml_error(400, "RequestTimeout"),
        xml_error(400, "Throttling"),
        (400, json.dumps({"__type": "com.amazonaws#ThrottlingException", "message": "rate"}), {}),
        xml_error(400, "RequestLimitExceeded"),
    ],
)
def test_transient_failures_are_retried(failure, pauses):
    t = Flaky([failure, failure])
    s3 = s3_client(t)
    assert s3.put_object(Bucket="bkt", Key="k", Body=b"payload") == {"ETag": ""}
    assert len(t.calls) == 3 and len(pauses) == 2
    assert all(c["body"] == b"payload" for c in t.calls)  # the same bytes every attempt


def test_retries_stop_after_five_attempts_with_growing_jittered_backoff(pauses):
    t = Flaky([xml_error(503, "SlowDown")] * 10)
    with pytest.raises(orch_aws.ClientError, match="SlowDown"):
        s3_client(t).get_object(Bucket="bkt", Key="k")
    assert len(t.calls) == 5 and len(pauses) == 4
    for i, p in enumerate(pauses):
        assert 0 <= p <= orch_aws.RETRY_BASE_SECONDS * 2**i


def test_a_network_error_on_the_last_attempt_is_raised(pauses):
    t = Flaky([ConnectionResetError(104, "reset")] * 5)
    with pytest.raises(ConnectionResetError):
        s3_client(t).get_object(Bucket="bkt", Key="k")
    assert len(t.calls) == 5


@pytest.mark.parametrize(
    "failure",
    [
        xml_error(403, "AccessDenied"),
        xml_error(404, "NoSuchKey"),
        xml_error(400, "InvalidRequest"),
        (400, json.dumps({"__type": "ResourceNotFoundException", "message": "no"}), {}),
        (307, "", {"location": "https://elsewhere/"}),
    ],
)
def test_other_client_errors_are_not_retried(failure, pauses):
    t = Flaky([failure])
    with pytest.raises(orch_aws.ClientError):
        s3_client(t).get_object(Bucket="bkt", Key="k")
    assert len(t.calls) == 1 and pauses == []


def test_a_certificate_failure_is_not_retried(pauses):
    import ssl

    t = Flaky([orch_aws.urllib.error.URLError(ssl.SSLCertVerificationError("bad cert"))] * 5)
    with pytest.raises(orch_aws.urllib.error.URLError):
        s3_client(t).get_object(Bucket="bkt", Key="k")
    assert len(t.calls) == 1


def test_each_attempt_is_signed_again_with_fresh_credentials(pauses):
    issued: list[str] = []

    def provider() -> orch_aws.Credentials:
        issued.append(f"AKID{len(issued)}")
        return orch_aws.Credentials(issued[-1], "s")

    t = Flaky([xml_error(500, "InternalError")])
    orch_aws.S3("eu-west-2", provider, t).get_object(Bucket="bkt", Key="k")
    assert issued == ["AKID0", "AKID1"]
    assert "Credential=AKID0/" in t.calls[0]["headers"]["Authorization"]
    assert "Credential=AKID1/" in t.calls[1]["headers"]["Authorization"]
    assert all("X-Amz-Date" in c["headers"] for c in t.calls)


def test_each_attempt_gets_a_fresh_signing_time(pauses, monkeypatch):
    clock = iter([NOW, NOW + dt.timedelta(seconds=7)])
    monkeypatch.setattr(orch_aws, "utcnow", lambda: next(clock))
    t = Flaky([xml_error(500, "InternalError")])
    s3_client(t).get_object(Bucket="bkt", Key="k")
    assert [c["headers"]["X-Amz-Date"] for c in t.calls] == ["20261102T103000Z", "20261102T103007Z"]


def test_a_body_that_is_not_bytes_is_sent_once(pauses):
    t = Flaky([xml_error(503, "SlowDown")])
    with pytest.raises(orch_aws.ClientError):
        s3_client(t).request("PutObject", "PUT", "https://bkt.s3.eu-west-2.amazonaws.com/k", {}, bytearray(b"x"))
    assert len(t.calls) == 1 and pauses == []


def test_complete_multipart_upload_error_inside_a_200_is_retried(pauses):
    t = Flaky(
        [(200, "<Error><Code>InternalError</Code><Message>retry</Message></Error>", {})],
        ok=(200, "<CompleteMultipartUploadResult><ETag>x</ETag></CompleteMultipartUploadResult>", {}),
    )
    s3_client(t).complete_multipart_upload(Bucket="bkt", Key="k", UploadId="u", MultipartUpload={"Parts": []})
    assert len(t.calls) == 2 and len(pauses) == 1


# --- names spliced into the endpoint host ----------------------------------------------------


@pytest.mark.parametrize(
    "region",
    ["eu-west-2", "us-east-1", "cn-north-1", "us-gov-west-1", "us-iso-east-1", "us-isob-east-1", "ap-southeast-4"],
)
def test_real_regions_are_accepted(region):
    assert orch_aws.S3(region, lambda: CREDS).region == region


@pytest.mark.parametrize(
    "region",
    ["", "eu-west", "EU-WEST-2", "eu-west-2.evil.example", "evil.example/x", "eu-west-2#", "eu-west-2 ", "e-west-2"],
)
def test_a_region_that_is_not_a_region_name_is_refused(region):
    for make in (orch_aws.S3, orch_aws.SecretsManager, orch_aws.Sts):
        with pytest.raises(ValueError, match="region"):
            make(region, lambda: CREDS)


@pytest.mark.parametrize("bucket", ["abc", "bucket-1", "my.bucket.name", "a" * 63, "0bucket9"])
def test_real_bucket_names_are_accepted(bucket, monkeypatch):
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    assert bucket in s3_client(FakeTransport([]))._url(bucket, "k")


@pytest.mark.parametrize(
    "bucket",
    [
        "",
        "ab",
        "a" * 64,
        "Bucket",
        "-bucket",
        "bucket-",
        "evil.example/x",
        "b@evil.example",
        "b:443",
        "b#",
        "b?x",
        "b b",
    ],
)
def test_a_bucket_that_is_not_a_bucket_name_is_refused(bucket, monkeypatch):
    for endpoint in ("", "https://s3.example.vpce.amazonaws.com"):
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", endpoint)
        s3 = s3_client(FakeTransport([]))
        with pytest.raises(ValueError, match="bucket"):
            s3.get_object(Bucket=bucket, Key="k")


# --- plain http only to the exact metadata and loopback hosts --------------------------------


@pytest.mark.parametrize(
    "url,kind",
    [
        ("http://169.254.169.254/latest/api/token", "metadata"),
        ("http://169.254.170.2/v2/credentials/x", "metadata"),
        ("http://169.254.170.23/v1/credentials", "metadata"),
        ("http://127.0.0.1:8080/b/k", "loopback"),
        ("http://localhost:4566/b/k", "loopback"),
        ("https://bkt.s3.eu-west-2.amazonaws.com/k", "aws"),
        ("https://127.0.0.1:8443/b/k", "aws"),
    ],
)
def test_endpoint_kinds(url, kind):
    assert orch_aws.endpoint_kind(url) == kind


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254.evil.example/latest/api/token",
        "http://169.254.170.2.evil.example/v2/credentials/x",
        "http://169.254.170.23.evil.example/creds",
        "http://169.254.169.254@evil.example/",
        "http://169.254.170.2:80@evil.example/x",
        "http://127.0.0.1:80@evil.example/",
        "http://127.0.0.10/",
        "http://localhost.evil.example:4566/",
        "http://s3.eu-west-2.amazonaws.com/b/k",
        "https://user:pw@bkt.s3.eu-west-2.amazonaws.com/k",
        "ftp://169.254.169.254/",
        "file:///etc/passwd",
        "https:///nohost",
    ],
)
def test_lookalike_metadata_and_loopback_hosts_are_refused(url):
    with pytest.raises(ValueError):
        orch_aws.http_send("GET", url, {})


@pytest.mark.parametrize(
    "full_uri",
    [
        "http://169.254.170.23.evil.example/creds",
        "http://169.254.170.2@evil.example/creds",
        "http://127.0.0.1.evil.example/creds",
        "http://127.0.0.1:1@evil.example/creds",
        "https://evil.example/creds",
    ],
)
def test_a_container_credentials_uri_on_another_host_is_ignored(full_uri, monkeypatch):
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", raising=False)
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", full_uri)
    t = FakeTransport([])
    assert orch_aws.container_credentials(t) is None
    assert t.calls == []


@pytest.mark.parametrize("full_uri", ["http://169.254.170.23/v1/credentials", "http://127.0.0.1:51679/creds"])
def test_a_container_credentials_uri_on_an_allowed_host_is_used(full_uri, monkeypatch):
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", raising=False)
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", full_uri)
    task = {"AccessKeyId": "ASIATASK", "SecretAccessKey": "s", "Token": "t"}
    t = FakeTransport([("GET", full_uri, (200, json.dumps(task), {}))])
    assert orch_aws.container_credentials(t).access_key == "ASIATASK"


@pytest.mark.parametrize("relative", ["@evil.example/creds", ".evil.example/creds", ":80@evil.example/x", "creds"])
def test_a_relative_container_uri_cannot_move_off_the_ecs_host(relative, monkeypatch):
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", relative)
    t = FakeTransport([])
    with pytest.raises(orch_aws.CredentialError):
        orch_aws.container_credentials(t)
    assert t.calls == []
