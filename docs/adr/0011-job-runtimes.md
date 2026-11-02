# ADR 0011 — Job runtimes: SQL in a Lambda zip by default, long jobs on SSM, Fargate optional

- Status: accepted

## Context

Jobs around a DMS task are mostly SQL: truncate the reset scope, DDL before and after the
load, counts, the pre-flight fingerprint. Some are not: `pg_dump`/`pg_restore`, DDL longer than
15 minutes, bash or pwsh scripts. Until now every SQL job that was not a Data API built-in ran
as `psql-run` in the Fargate `sql-runner` image. The target platform builds machines from team
AMIs, takes container images only from pre-mirrored ECR repositories and deploys Lambda
functions as zips it can inspect. An image on the default path means an ECS cluster, ECR
endpoints, an image build with downloads at build time (review finding 12) and a connections
file with plaintext passwords in the task. Many targets have no Data API.

Options for the command jobs:

1. Fargate only: an image the platform must build or import, a cluster and ECR endpoints in
   every deployment.
2. SSM Run Command on instances from the team AMI: the platform's own machines and tooling, no
   image; the job-runner polls the command (option 1 of ADR 0002).
3. Lambda only: no command runtime; dumps by cluster snapshot (ADR 0007), restore by hand.

## Decision

Three runtimes, chosen per job (`runtime: lambda | ssm | fargate`):

* **Lambda (default for SQL).** A job with `sql:` (`file`, `inline` or `truncateScope`) runs in
  the module's `sql_jobs` Lambda (`lambda.sql_jobs_enabled`, default true; 900 s, 1024 MB, the
  jobs role). The driver is pg8000, pure Python, vendored into the Lambda zip from hashed
  requirements (`uv pip install --require-hashes --only-binary :all:`); `make vendor-check`
  rebuilds it in CI and compares every file. Nothing is downloaded at run time.
* **SSM (long jobs).** `pg-dump`, `orch.py restore`, long DDL and scripts run through SSM Run
  Command on instances built from the team AMI, one runner per project (`ssm.enabled`,
  `ssm.runners`: per project `instance_ids` or `target_tags` and the instance role; for
  example one dms-sql-bastion per project). The module ships the runtime as a zip in the
  artifacts bucket; the document checks its sha256 and runs orch-job as `ssm.runner_user`,
  which assumes the project's job role from the instance profile. The runtime needs python3
  with the standard library only (no boto3, no AWS CLI on the AMI): `orch_aws.py` signs its
  requests with SigV4, and the document carries it to fetch the zip. Off by default.
* **Fargate (optional).** The `sql-runner` image stays for whoever prefers containers: with a
  non-empty `fargate.images` the module creates the ECS cluster (unless `fargate.cluster_arn`),
  the task definitions and the ECS roles, and the network contract asks for the ECR endpoints.
  With the default `fargate = {}` none of it exists and `ecs_cluster_arn` is null.
* **The module never builds an image.** It takes a Lambda zip it packages from the repository
  (or a prebuilt one, `lambda.artifact_s3_*`) and, for Fargate, a pre-pushed image URI, pinned
  by digest. Whoever builds the image builds it from the hardened Dockerfile
  (`modules/dms-orchestration/src/jobs/sql-runner/Dockerfile`): base image pinned by digest through `BASE_IMAGE`, every
  source a build arg for internal mirrors (`PGDG_APT_URL`, `PGDG_GPG_URL`, `PIP_INDEX_URL`,
  `RDS_CA_URL`, `POWERSHELL_DEB_URL`), every download checked (PGDG key fingerprint, CA bundle
  and PowerShell `.deb` by sha256), Python wheels by hash only, PowerShell off by default. CI
  builds and scans it only when the repository variable `BUILD_SQL_RUNNER_IMAGE` is `true`; the
  sandbox CodeBuild project (`examples/sandbox/sql_runner.tf`) is a sandbox-only convenience,
  off by default.
* **TLS and login.** `verify-full` (or `verify-ca`) against the RDS CA bundle, pinned by the
  same sha256 in the Lambda zip and in the image; other `sslMode` values are refused in Lambda.
  The secret or an IAM token is read as the project job role.
* **Semantics in Lambda.** Each file is one message, so one implicit transaction;
  `singleTransaction` joins all files of a job. Templates become SQL literals or quoted
  identifiers. A dry run opens a read-only session, so a write fails with
  `ReadOnlySqlTransaction`. `truncateScope` reads the full reset list from the job's
  `spec.json`, also above the payload cap.
* **Built-ins.** The SQL built-ins, the pre-flight fingerprint included, use pg8000 when the
  connection has a host and the Data API otherwise, with the same results and the same
  fingerprint hash. The target check therefore runs in Lambda for every reachable target, and
  goes to a command runtime only when the baseline is too big for 15 minutes.

### Polling the SSM command versus ADR 0002

ADR 0002 rejected a polling loop for the DMS wait: a load can take days, and a 30-second loop
over days would spend transitions and approach the 25,000-event history limit. The SSM job
loop (`RunSsmJob` → `WaitSsm` 30 s → `GetSsmInvocation`) accepts that trade-off for a narrower
case: a job runs minutes to hours, in its own job-runner execution, so the loop stays far below
the history limit and costs a few transitions a minute. SSM offers no task-token callback
without an extra relay (EventBridge rule, Lambda, a token store), which would bring back the
moving parts ADR 0002 needed a watchdog for. Bastion-style polling is therefore confined to the
job-runner; the run machines still never poll DMS. The loop is bounded: the document stops the
script at the job's `timeoutSeconds`, and the job-runner gives up 900 s later, so a lost
command fails the job instead of hanging the run.

## Consequences

* The default deployment needs no ECS cluster, no ECS roles, no ECR endpoints and no image; the
  `no_fargate_images` advisory is gone (`defaults.tftest.hcl::no_ecs_resources_by_default`,
  `options.tftest.hcl::fargate_optional`).
* A SQL job in Lambda is limited to 840 s. Longer work is an SSM (or Fargate) job.
* SQL in Lambda behaves differently from `psql`: a file commits as a whole,
  `CREATE INDEX CONCURRENTLY` and `VACUUM` need a file of their own, psql meta-commands do not
  exist.
* The vendored driver and the CA bundle are updated by changing `requirements.in` or the pin,
  running `make vendor` and committing; CI refuses a tree that differs. The image's Python
  dependencies follow the same rule (`modules/dms-orchestration/src/jobs/sql-runner/requirements.in` compiled with
  hashes).
* The zip of every module Lambda carries the vendored driver (about 3 MB unpacked).
* **SSM runners are per project, never shared (0.3).** 0.2 had one runner selection and one
  instance role for all projects, so a job of one project on a runner could act as another
  project's job role. Now `ssm.runners` has one runner per project: its job role trusts only
  that runner's instance role, the runner's policy (`ssm_runner_policy_json`, key `<project>`)
  assumes only that job role, the job-runner sends a job only to its project's runner, and the
  module refuses a shared instance role or instance. What remains is per project: anyone with a
  session or `SendCommand` on a project's runner can use that project's job role and read a
  running job's files ([security.md](../security.md)); keep session access to each runner as
  narrow as access to its project's job role.
* **The runner runtime has no third-party Python.** The team AMI has python3 with the standard
  library, the PostgreSQL clients and the SSM agent; no boto3 and no AWS CLI. The runtime's AWS
  calls (S3, Secrets Manager, STS, the RDS IAM token) go through `orch_aws.py`, a SigV4 client
  on the standard library whose signatures the tests compare with botocore's. Credentials stay
  where they were: the job profile names the job role and the instance profile as its source,
  and the client assumes the role (IMDSv2) and renews it before it expires. Moving the AWS calls
  to the Lambda side instead was rejected: secrets would travel through SSM parameters (visible
  in the command history), and multi-hour dumps need fresh credentials for every upload part.
