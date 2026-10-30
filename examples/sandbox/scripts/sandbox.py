#!/usr/bin/env python3
"""Sandbox helper: seed data, upload the job set, look at DMS and run timelines.

    sandbox.py seed                       # schema + data on the source, dms_source password
    sandbox.py test-connections           # MaxFileSize 0 fix on PG targets, TestConnection of every endpoint
    sandbox.py sync-jobs                  # jobs/jobset*.yaml.tmpl as jobs/ledger/e2e/, e2e-dms-only/
    sandbox.py build-image <git-sha>      # CodeBuild: sql-runner image -> ECR; fails on HIGH/CRITICAL
    sandbox.py sync-jobs-fargate          # jobs/jobset-fargate.yaml.tmpl as jobs/ledger/e2e-fargate/
    sandbox.py sync-jobs-ssm              # jobs/jobset-ssm.yaml.tmpl as jobs/ledger/e2e-ssm/ (ssm_runner)
    sandbox.py sync-jobs-iam              # jobs/jobset-iam.yaml.tmpl as jobs/ledger/e2e-iam/ (iam_auth)
    sandbox.py counts                     # row counts source vs target
    sandbox.py sql <source|target> "SQL"  # one statement
    sandbox.py task                       # DescribeReplicationTasks summary + table statistics
    sandbox.py timeline <runId>           # RUN events from DynamoDB in order
    sandbox.py migration                  # the MIGRATION item of the task (armed / claimed / posted)
    sandbox.py fetch-upstream [--ref SHA] # upstream aurora/dms modules -> upstream/ (see upstream.env.example)

Reads `terraform output -json` of this directory (fetch-upstream does not). Credentials: AWS_PROFILE.
Run with pg8000 available: `uv run --with pg8000 python examples/sandbox/scripts/sandbox.py ...`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
TABLES = ["app.customers", "app.accounts", "app.transactions", "app.audit_notes"]


def outputs() -> dict[str, Any]:
    raw = subprocess.run(["terraform", "output", "-json"], cwd=HERE, check=True, capture_output=True, text=True).stdout
    return {k: v["value"] for k, v in json.loads(raw).items()}


def session(out: dict[str, Any]) -> boto3.session.Session:
    return boto3.session.Session(region_name=out["region"])


def split_sql(text: str) -> list[str]:
    """Split on ';' outside of $$ bodies and quotes; full-line comments are dropped first."""
    text = chr(10).join(ln for ln in text.splitlines() if not ln.strip().startswith("--"))
    out, buf, in_dollar, in_quote = [], [], False, False
    i = 0
    while i < len(text):
        if text.startswith("$$", i) and not in_quote:
            in_dollar = not in_dollar
            buf.append("$$")
            i += 2
            continue
        ch = text[i]
        if ch == "'" and not in_dollar:
            in_quote = not in_quote
        if ch == ";" and not in_dollar and not in_quote:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    stmts = []
    for chunk in out:
        lines = [ln for ln in chunk.splitlines() if not ln.strip().startswith("--")]
        sql = "\n".join(lines).strip()
        if sql:
            stmts.append(sql)
    return stmts


def connect(out: dict[str, Any], db: str, secret: str | None = None) -> Any:
    """pg8000 connection to 'source' or 'target' (TLS without CA check: sandbox only)."""
    import ssl

    import pg8000.native

    sec = json.loads(
        session(out).client("secretsmanager").get_secret_value(SecretId=out["secrets"][secret or db])["SecretString"]
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return pg8000.native.Connection(
        user=sec["username"],
        password=sec["password"],
        host=sec["host"],
        port=int(sec["port"]),
        database=sec["dbname"],
        ssl_context=ctx,
        timeout=600,
    )


def data_api(out: dict[str, Any], db: str, sql: str, secret: str | None = None) -> list[dict[str, Any]]:
    """Run one statement on 'source' or 'target'."""
    conn = connect(out, db, secret)
    try:
        rows = conn.run(sql)
        cols = [c["name"] for c in conn.columns or []]
        return [dict(zip(cols, r, strict=False)) for r in rows or []]
    finally:
        conn.close()


def cmd_seed(out: dict[str, Any]) -> None:
    seed = REPO / "tests" / "e2e" / "seed"
    for f in sorted(seed.glob("*.sql")):
        for sql in split_sql(f.read_text()):
            t0 = time.monotonic()
            data_api(out, "source", sql)
            print(f"{f.name}: {sql.splitlines()[0][:70]} ({time.monotonic() - t0:.1f}s)")
    password = subprocess.run(
        ["terraform", "output", "-raw", "dms_source_password"], cwd=HERE, check=True, capture_output=True, text=True
    ).stdout.strip()
    data_api(out, "source", f"ALTER ROLE dms_source WITH LOGIN PASSWORD '{password}'")
    print("dms_source password set")
    # The task uses target_table_prep_mode = DO_NOTHING and the upstream dms module puts the DMS
    # control tables into dms_control: both must exist on the target before the first run.
    for sql in [*split_sql((seed / "010_source_schema.sql").read_text()), "CREATE SCHEMA IF NOT EXISTS dms_control"]:
        data_api(out, "target", sql)
    print("target schema and dms_control created")
    cmd_counts(out)


def cmd_counts(out: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for table in TABLES:
        row = {}
        for db in ("source", "target"):
            try:
                row[db] = data_api(out, db, f"SELECT count(*)::bigint AS n FROM {table}")[0]["n"]  # noqa: S608 - fixed table list
            except Exception as err:  # noqa: BLE001 - table may not exist on the target yet
                row[db] = f"n/a ({type(err).__name__})"
        result[table] = row
        print(f"{table:20} source={row['source']!s:>8} target={row['target']!s:>8}")
    return result


def cmd_sync_jobs(out: dict[str, Any], template: str = "jobset.yaml.tmpl", version: str = "e2e") -> None:
    text = (HERE / "jobs" / template).read_text().replace("__SQL_LAMBDA_ARN__", out["sql_lambda_arn"])
    with tempfile.TemporaryDirectory() as tmp:
        bundle, built = Path(tmp) / "bundle", Path(tmp) / "built"
        bundle.mkdir()
        (bundle / "jobset.yaml").write_text(text)
        subprocess.run(
            [sys.executable, str(REPO / "scripts" / "jobset_tool.py"), "build", str(bundle), str(built)], check=True
        )
        s3 = session(out).client("s3")
        for path in built.rglob("*"):
            if path.is_file():
                key = f"jobs/ledger/{version}/{path.relative_to(built).as_posix()}"
                s3.upload_file(str(path), out["artifacts_bucket"], key)
                print(f"uploaded s3://{out['artifacts_bucket']}/{key}")


BLOCKING_SEVERITIES = ("CRITICAL", "HIGH")


def scan_blocks(counts: dict[str, int]) -> list[str]:
    """HIGH/CRITICAL in the ECR basic scan. It cannot leave out unfixed CVEs, so it is
    reported; the gate is trivy in the build itself, with the CI policy (ignore-unfixed)."""
    return [s for s in BLOCKING_SEVERITIES if counts.get(s, 0) > 0]


BUILD_SOURCE_KEY = "codebuild/sql-runner-src.zip"  # sql_runner.tf: aws_s3_object.sql_runner_src


def stale_build_sources(archive: bytes, repo: Path) -> list[str]:
    """Files of the CodeBuild source zip whose content differs from the working tree (or that
    the tree no longer has). The zip is what the last ``terraform apply`` uploaded."""
    import io
    import zipfile

    stale = []
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        for name in sorted(z.namelist()):
            if name == "buildspec.yml" or name.endswith("/"):
                continue
            local = repo / name
            if not local.is_file() or local.read_bytes() != z.read(name):
                stale.append(name)
    return stale


def cmd_build_image(out: dict[str, Any], tag: str) -> int:
    """CodeBuild builds the sql-runner image (sql_runner.tf) and pushes it only if trivy
    finds no fixable HIGH/CRITICAL; ECR then scans it on push (report only). Refuses when the
    source zip in S3 is older than the working tree: the tags are immutable, so a build from
    old sources would publish old code under the new tag for good."""
    s = session(out)
    archive = s.client("s3").get_object(Bucket=out["artifacts_bucket"], Key=BUILD_SOURCE_KEY)["Body"].read()
    stale = stale_build_sources(archive, REPO)
    if stale:
        print(
            f"the build source in S3 differs from the working tree ({', '.join(stale[:5])}): run terraform apply first"
        )
        return 2
    cb, ecr = s.client("codebuild"), s.client("ecr")
    build = cb.start_build(
        projectName=out["sql_runner_codebuild_project"],
        environmentVariablesOverride=[{"name": "IMAGE_TAG", "value": tag, "type": "PLAINTEXT"}],
    )["build"]
    print("build", build["id"])
    while build["buildStatus"] == "IN_PROGRESS":
        time.sleep(15)
        build = cb.batch_get_builds(ids=[build["id"]])["builds"][0]
    print("build", build["buildStatus"], [(p["phaseType"], p.get("phaseStatus")) for p in build.get("phases", [])])
    if build["buildStatus"] != "SUCCEEDED":
        return 1
    repo, image = out["sql_runner_repository"], {"imageTag": tag}
    deadline = time.monotonic() + 600
    while True:  # the scan record appears some seconds after the push
        try:
            findings = ecr.describe_image_scan_findings(repositoryName=repo, imageId=image)
            if findings["imageScanStatus"]["status"] in ("COMPLETE", "FAILED"):
                break
        except ecr.exceptions.ScanNotFoundException:
            pass
        if time.monotonic() > deadline:
            print("ECR scan: no result in 10 min")
            return 0
        time.sleep(10)
    counts = findings["imageScanFindings"].get("findingSeverityCounts", {})
    print("ECR basic scan", findings["imageScanStatus"]["status"], json.dumps(counts), "blocking:", scan_blocks(counts))
    for f in findings["imageScanFindings"].get("findings", []):
        if f["severity"] in BLOCKING_SEVERITIES:
            pkg = {a["key"]: a["value"] for a in f.get("attributes", [])}
            print(f"  {f['severity']:8} {f['name']} {pkg.get('package_name')} {pkg.get('package_version')}")
    return 0


DMS_DEFAULT_MAX_FILE_SIZE_KB = 32768


def fix_target_max_file_size(dms: Any, endpoint_arns: list[str]) -> list[str]:
    """Raise MaxFileSize = 0 on PostgreSQL targets to the DMS default; return the fixed ARNs.

    The upstream dms module always renders postgres_settings for a PostgreSQL target, and the
    provider then sends max_file_size = 0 (it is in state as 0). DMS 3.5.4 with that loads
    0 rows into the target and never finishes (seen live, docs/dms-observations.md).
    The module does not expose the field; until it does, set it through the API. That
    resets the endpoint's connection test, so run this before TestConnection.
    """
    fixed = []
    for arn in endpoint_arns:
        for e in dms.describe_endpoints(Filters=[{"Name": "endpoint-arn", "Values": [arn]}])["Endpoints"]:
            settings = e.get("PostgreSQLSettings") or {}
            if (
                e["EndpointType"].upper() == "TARGET"
                and e["EngineName"] in ("postgres", "aurora-postgresql")
                and settings.get("MaxFileSize") == 0
            ):
                dms.modify_endpoint(EndpointArn=arn, PostgreSQLSettings={"MaxFileSize": DMS_DEFAULT_MAX_FILE_SIZE_KB})
                fixed.append(arn)
    return fixed


def start_connection_test(dms: Any, ri: str, arn: str) -> bool:
    """TestConnection; False if a test is already running (ModifyEndpoint starts one itself)."""
    try:
        dms.test_connection(ReplicationInstanceArn=ri, EndpointArn=arn)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "InvalidResourceStateFault":
            raise
        return False
    return True


def cmd_test_connections(out: dict[str, Any]) -> None:
    dms = session(out).client("dms")
    ri = out["replication_instance_arn"]
    for arn in fix_target_max_file_size(dms, list(out["endpoint_arns"].values())):
        print(f"MaxFileSize 0 -> {DMS_DEFAULT_MAX_FILE_SIZE_KB} KB on {arn}")
    for arn in out["endpoint_arns"].values():
        start_connection_test(dms, ri, arn)
    deadline = time.monotonic() + 600
    while True:
        conns = dms.describe_connections(Filters=[{"Name": "replication-instance-arn", "Values": [ri]}])["Connections"]
        state = {c["EndpointIdentifier"]: (c["Status"], c.get("LastFailureMessage")) for c in conns}
        if all(s != "testing" for s, _ in state.values()) or time.monotonic() > deadline:
            break
        time.sleep(10)
    for name, (status, msg) in sorted(state.items()):
        print(f"{name:30} {status} {msg or ''}")


def cmd_task(out: dict[str, Any]) -> None:
    dms = session(out).client("dms")
    task = dms.describe_replication_tasks(
        Filters=[{"Name": "replication-task-arn", "Values": [out["task_arn"]]}], WithoutSettings=True
    )["ReplicationTasks"][0]
    print(json.dumps(task, indent=2, default=str))
    stats = dms.describe_table_statistics(ReplicationTaskArn=out["task_arn"])["TableStatistics"]
    for s in stats:
        print(f"{s['SchemaName']}.{s['TableName']:15} {s['TableState']:20} rows={s.get('FullLoadRows')}")


def cmd_timeline(out: dict[str, Any], run_id: str) -> None:
    table = session(out).resource("dynamodb").Table(out["table_name"])
    items = table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq(f"RUN#{run_id}"))["Items"]
    for item in sorted(items, key=lambda i: i["sk"]):
        if item["sk"] == "META":
            print(
                "META",
                json.dumps({k: v for k, v in item.items() if k in ("status", "outcome", "reportS3Uri")}, default=str),
            )
        else:
            print(
                item["sk"][:40],
                item.get("source"),
                item.get("phase"),
                str(item.get("detail") or item.get("status"))[:300],
            )


def cmd_migration(out: dict[str, Any]) -> None:
    table = session(out).resource("dynamodb").Table(out["table_name"])
    item = table.get_item(Key={"pk": f"MIGRATION#{out['task_arn']}", "sk": "ACTIVE"}).get("Item")
    print(json.dumps(item, indent=2, default=str))


# --- S16: SQS fan-out -------------------------------------------------------


def filter_matches(policy: dict[str, list[str]], attributes: dict[str, dict[str, str]]) -> bool:
    """SNS filter policy on MessageAttributes, the exact-string subset fanout-filter-policy.json
    uses: every key must be present with one of the listed values."""
    return all(
        key in attributes and attributes[key].get("StringValue", attributes[key].get("Value")) in allowed
        for key, allowed in policy.items()
    )


def drain_queue(sqs: Any, url: str, wait_seconds: int = 20, empty_polls: int = 3) -> list[dict[str, Any]]:
    """Read and delete everything in the queue (SNS envelopes, raw delivery off) until it stays
    empty for ``empty_polls`` long polls. Returns runId, status, severity and the attributes."""
    got: list[dict[str, Any]] = []
    empty = 0
    while empty < empty_polls:
        msgs = (
            sqs.receive_message(
                QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=wait_seconds, MessageAttributeNames=["All"]
            ).get("Messages")
            or []
        )
        empty = 0 if msgs else empty + 1
        for m in msgs:
            env = json.loads(m["Body"])
            body = json.loads(env.get("Message") or "{}")
            attrs = {k: v.get("Value") for k, v in (env.get("MessageAttributes") or {}).items()}
            got.append(
                {
                    "runId": body.get("runId"),
                    "status": body.get("status"),
                    "severity": body.get("severity"),
                    "attributes": attrs,
                }
            )
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
    return got


def fanout_checks(runs: dict[str, str], got: list[dict[str, Any]]) -> dict[str, bool]:
    """runs: status -> runId of the run that ended so. FAILED and MANUAL must arrive, SUCCEEDED
    must not, and nothing below severity high gets through."""
    by_run = {m["runId"]: m for m in got}
    return {
        "FAILED delivered": (by_run.get(runs["FAILED"]) or {}).get("status") == "FAILED",
        "MANUAL delivered": (by_run.get(runs["MANUAL"]) or {}).get("status") == "MANUAL",
        "SUCCEEDED not delivered": runs["SUCCEEDED"] not in by_run,
        "only severity high": all(m["attributes"].get("severity") == "high" for m in got),
    }


ALREADY_EXISTS = "StepFunctions.ExecutionAlreadyExistsException"


def resume_collision_checks(events: list[dict[str, Any]]) -> dict[str, bool]:
    """idem-sfn: the post history after its StartResume met an execution of the same name.
    The Catch in post.asl.json names ALREADY_EXISTS; this proves AWS raises exactly that and
    that the post took the "already started" branch (WriteReport), not GiveLockBack."""
    entered = [e["stateEnteredEventDetails"]["name"] for e in events if "stateEnteredEventDetails" in e]
    errors = [e["taskFailedEventDetails"]["error"] for e in events if "taskFailedEventDetails" in e]
    after = entered[entered.index("StartResume") + 1 :] if "StartResume" in entered else []
    return {
        "StartResume reached": "StartResume" in entered,
        f"real error {ALREADY_EXISTS}": ALREADY_EXISTS in errors,
        "caught as already started (WriteReport next)": after[:1] == ["WriteReport"],
        "lock not taken back (no GiveLockBack)": "GiveLockBack" not in entered,
        "post did not fail": not any(e.get("type") == "ExecutionFailed" for e in events),
    }


############################################
# Upstream modules: aurora and dms come from a repository named only in the environment
# (UPSTREAM_MODULES_GIT / UPSTREAM_MODULES_REF, or the gitignored upstream.env). Nothing in
# git points at a concrete implementation; docs/upstream-modules-contract.md says what it
# has to provide.
############################################

UPSTREAM_PATHS = ("modules/aurora", "modules/dms")
SHA = re.compile(r"[0-9a-f]{40}")


class UpstreamError(Exception):
    pass


def upstream_config(env: Mapping[str, str], env_file: Path) -> tuple[str, str]:
    """(url, ref): the environment first, then KEY=VALUE lines of env_file."""
    values: dict[str, str] = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                values[key.strip()] = value.strip().strip("'\"")
    pick = {k: env.get(k) or values.get(k, "") for k in ("UPSTREAM_MODULES_GIT", "UPSTREAM_MODULES_REF")}
    return pick["UPSTREAM_MODULES_GIT"], pick["UPSTREAM_MODULES_REF"]


def _git(*args: str, cwd: Path | None = None, what: str) -> None:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        # git's stderr may echo the URL; report the step only.
        raise UpstreamError(f"git failed: {what} (exit {res.returncode})")


def _rmtree(path: Path) -> None:
    def writable(func: Any, p: str, _exc: BaseException) -> None:  # read-only git objects on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onexc=writable)


def fetch_upstream(url: str, ref: str, dest: Path, patches_dir: Path | None = None) -> str:
    """Sparse checkout of modules/aurora and modules/dms at a pinned commit into dest.

    Applies patches_dir/*.patch in name order and writes dest/.ref (the commit, then the
    applied patches). Returns the commit. Never prints or raises the URL.
    """
    if not url:
        raise UpstreamError("UPSTREAM_MODULES_GIT is not set (environment or upstream.env, see upstream.env.example)")
    if url.startswith("-"):
        raise UpstreamError("UPSTREAM_MODULES_GIT is not a repository URL")
    if not SHA.fullmatch(ref or ""):
        raise UpstreamError("UPSTREAM_MODULES_REF must be a 40-character commit sha (lowercase hex)")
    if dest.exists():
        _rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "--quiet", "--filter=blob:none", "--sparse", "--no-checkout", "--", url, str(dest), what="clone")
    _git("sparse-checkout", "set", *UPSTREAM_PATHS, cwd=dest, what="sparse-checkout")
    try:
        _git("checkout", "--quiet", "--detach", ref, cwd=dest, what="checkout")
    except UpstreamError:
        # Not reachable from a branch head: fetch the commit itself.
        _git("fetch", "--quiet", "origin", ref, cwd=dest, what=f"fetch {ref}")
        _git("checkout", "--quiet", "--detach", ref, cwd=dest, what=f"checkout {ref}")
    missing = [p for p in UPSTREAM_PATHS if not (dest / p).is_dir()]
    if missing:
        raise UpstreamError(f"commit {ref} has no {', '.join(missing)}")
    applied = []
    for patch in sorted(patches_dir.glob("*.patch")) if patches_dir and patches_dir.is_dir() else []:
        _git("apply", str(patch.resolve()), cwd=dest, what=f"apply {patch.name}")
        applied.append(patch.name)
    (dest / ".ref").write_text("\n".join([ref, *applied]) + "\n", encoding="utf-8")
    return ref


def cmd_fetch_upstream(args: list[str]) -> int:
    url, ref = upstream_config(os.environ, HERE / "upstream.env")
    if args[:1] == ["--ref"] and len(args) == 2:
        ref = args[1]
    elif args:
        print("usage: sandbox.py fetch-upstream [--ref <commit-sha>]", file=sys.stderr)
        return 2
    try:
        sha = fetch_upstream(url, ref, HERE / "upstream", HERE / "upstream-patches")
    except UpstreamError as err:
        print(f"fetch-upstream: {err}", file=sys.stderr)
        return 2
    print(f"upstream modules at {sha} in {HERE / 'upstream'}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "fetch-upstream":
        return cmd_fetch_upstream(argv[1:])
    out = outputs()
    cmd, args = argv[0], argv[1:]
    if cmd == "seed":
        cmd_seed(out)
    elif cmd == "test-connections":
        cmd_test_connections(out)
    elif cmd == "sync-jobs":
        cmd_sync_jobs(out)
        cmd_sync_jobs(out, "jobset-dms-only.yaml.tmpl", "e2e-dms-only")
        cmd_sync_jobs(out, "jobset-postfail.yaml.tmpl", "e2e-postfail")
    elif cmd == "sync-jobs-fargate":
        cmd_sync_jobs(out, "jobset-fargate.yaml.tmpl", "e2e-fargate")
    elif cmd == "sync-jobs-ssm":
        cmd_sync_jobs(out, "jobset-ssm.yaml.tmpl", "e2e-ssm")
    elif cmd == "sync-jobs-iam":
        cmd_sync_jobs(out, "jobset-iam.yaml.tmpl", "e2e-iam")
    elif cmd == "build-image":
        return cmd_build_image(out, args[0])
    elif cmd == "counts":
        cmd_counts(out)
    elif cmd == "sql":
        print(json.dumps(data_api(out, args[0], args[1]), indent=2, default=str))
    elif cmd == "task":
        cmd_task(out)
    elif cmd == "timeline":
        cmd_timeline(out, args[0])
    elif cmd == "migration":
        cmd_migration(out)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
