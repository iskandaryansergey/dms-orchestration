# ADR 0011 — Job runtimes: SQL in a Lambda zip by default, commands on SSM or Fargate

- Status: proposed (draft; completed with the SSM runtime and the optional Fargate runtime)

## Context

Jobs around a DMS task are mostly SQL: truncate the reset scope, DDL before and after the
load, counts, the pre-flight fingerprint. Until now every SQL job that was not a Data API
built-in ran as `psql-run` in the Fargate `sql-runner` image. The target platform builds
machines from team AMIs, takes container images only from pre-mirrored ECR repositories and
deploys Lambda functions as zips it can inspect. An image on the default path means an ECS
cluster, ECR endpoints, an image build with downloads at build time (finding 12) and a
connections file with plaintext passwords in the task. Many targets have no Data API.

## Decision

* **Lambda SQL runtime (default for SQL).** A job with `sql:` (`file`, `inline` or
  `truncateScope`) runs in the module's `sql_jobs` Lambda (`lambda.sql_jobs_enabled`, default
  true; 900 s, 1024 MB, the jobs role). The driver is pg8000, pure Python, vendored into the
  Lambda zip from hashed requirements (`uv pip install --require-hashes --only-binary :all:`);
  `make vendor-check` rebuilds it in CI and compares every file. Nothing is downloaded at run
  time.
* **TLS and login.** `verify-full` (or `verify-ca`) against the RDS CA bundle shipped in the zip
  and pinned by sha256; other `sslMode` values are refused. The secret or an IAM token is read
  as the project job role and stays in memory: no file, no environment variable.
* **Semantics.** Each file is one message, so one implicit transaction; `singleTransaction`
  joins all files of a job. Templates become SQL literals or quoted identifiers. A dry run opens
  a read-only session, so a write fails with `ReadOnlySqlTransaction`. `truncateScope` reads the
  full reset list from the job's `spec.json`, also above the payload cap.
* **Built-ins.** The SQL built-ins, the pre-flight fingerprint included, use pg8000 when the
  connection has a host and the Data API otherwise, with the same results and the same
  fingerprint hash. The target check therefore runs in Lambda for every reachable target, and
  goes to a command runtime only when the baseline is too big for 15 minutes.
* **Command runtimes.** `runtime` is `lambda | ssm | fargate`. Long jobs (pg_dump and
  pg_restore, long DDL, bash or pwsh scripts) run on an SSM-managed instance built from the
  team AMI; Fargate stays optional, off unless images are configured. The module never builds
  an image.

## Consequences

* The default deployment needs no ECS cluster, no ECR endpoints and no image; the
  `no_fargate_images` advisory is gone.
* A SQL job is limited to 840 s. Longer work is a command job.
* SQL behaves differently from `psql`: a file commits as a whole, `CREATE INDEX CONCURRENTLY`
  and `VACUUM` need a file of their own, psql meta-commands do not exist.
* The vendored driver and the CA bundle are updated by changing `requirements.in` or the pin,
  running `make vendor` and committing; CI refuses a tree that differs.
* The zip of every module Lambda carries the vendored driver (about 3 MB unpacked).
