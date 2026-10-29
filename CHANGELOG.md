# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions: SemVer (0.x until the
first production run). Commit references name the commit that made or fixed the item; the
observed AWS behaviour behind the fixes is in [docs/dms-observations.md](docs/dms-observations.md).

## [0.1.0] — 2026-10-29

The first release.

### Added

**Module and state machines**
- Terraform module `modules/dms-orchestration` (Terraform 1.9.8, aws `>= 5.90, < 6`): DynamoDB
  single table, S3 artifacts bucket, SNS topic, SSM task registrations, Lambda functions in the
  VPC, EventBridge rules for DMS task state and execution status with an SQS DLQ, watchdog
  schedule, ECS cluster and job task definitions, IAM per component, alarms, advisory checks,
  outputs for operators, the network and the KMS key policy.
- Two Standard state machines in JSONata (ADR 0005). `migrate`: lock → inspect → pre-flight →
  pre-jobs → assessment → read-only Arm guard → dump → reset → save context → arm `MIGRATION#` →
  `StartReplicationTask` → report `STARTED` → end; it never waits for DMS. `post`, started by the
  DMS event: load the saved run → take over the lock → post-jobs or the failure branch → baseline →
  report → notify → unlock. `job-runner` runs one job in a Lambda or on Fargate. Auto-resume is a
  new `migrate` (`<runId>-r<n>`, lock handed over, bounded by `autoResume.maxAttempts`); a dry run
  is handed to `post` without DMS.
- Handoff (`src/shared/handoff.py`): `dms_event_handler` and `watchdog` claim `MIGRATION#` once and
  start `post` under a fixed name (`<runId>-post`); a stale claim is repeated after 2 min;
  `ExecutionAlreadyExists` counts as started. `release_lock` on failed or aborted executions;
  watchdog alarm `MigrationOverdue` after the task's `runTimeoutSeconds`.
- Pre-flight (ADR 0006): the target is fingerprinted before the start (tables, columns, PK/unique,
  FKs, sequences, rows, row counters, in the scope of the table mapping) and compared with the
  expectation from the source and the baseline of the last run: `ready | prepare | resume | reset |
  manual | reject`. A spoiled target, rows in the scope of a new or recreated task, a completed task
  or a restored target stop the run as `MANUAL` with the diff; `allowReset` turns that into a full
  reset of the scope. `post` records the baseline (`TARGET#`) and a copy of the table statistics
  (`TABLE#`) at every end. `orch.py gate` for promotion.
- The target check runs in a Lambda (`checks/target_verify`, Data API) or in psql on Fargate
  (`verify-target`), one fingerprint from the same SQL (`src/shared/fingerprint.py`):
  `preflight.verifyRuntime = auto | lambda | fargate` with `fargateAboveTables` and
  `fargateAboveBytes` (ADR 0008).
- Configuration checks before the start: the table mapping has an include rule, the task uses
  `TargetTablePrepMode = DO_NOTHING`, a CDC task can stop, and the PostgreSQL target endpoint has
  no `MaxFileSize = 0`. Optional DMS premigration assessment before the Arm guard
  (`assessment`, `fail_on_warning`); a refused assessment leaves the target untouched.
- Target dumps before a reset: `pg-dump-s3` / `pg-restore-s3` in the `sql-runner` image
  (streamed multipart upload, SSE-KMS, manifest with SHA-256), `orch.py dumps`, `orch.py restore`
  with a restore baseline. `dump.method = snapshot` (ADR 0007): a tagged cluster
  snapshot of the target, polled until available, `runs/<runId>/dump.json`, post job
  `snapshot-retention`; job role rights scoped by the project tag; the report advises a snapshot
  above `dump.snapshotAboveBytes`. Restore from a snapshot stays with the database team.
- IAM database authentication for Fargate jobs (ADR 0009): `connections[*].auth = "iam"`
  with `iam_user` and `iam_policy_arn`; the upstream aurora module's `rds-db:connect` policy is
  attached to the project's job role; a token per psql start, only in `PGPASSWORD`, TLS required.
  DMS and the Data API keep their secrets.
- Repeat of post: `orch.py post <runId> [--from-job] [--jobs-version]` asks DMS for the outcome
  again, takes the lock and skips the post jobs already `ok`.
- SNS message attributes `environment, project, taskKey, status, phase, severity` on every
  notification, so subscriptions of the platform topic filter without parsing; `table_name` and
  `artifacts_bucket_name` bring state from a long-lived workspace; checks
  `own_sns_topic_outside_sandbox` and `state_in_the_dms_workspace`; `container_insights` (off).

**Jobs and tools**
- Job sets: schema and contract, `prepare_job`, built-in Lambda jobs (connections with
  `TestConnection`, PostgreSQL grants, row counts, target check, cluster snapshots, DMS
  diagnostics, control-table cleanup), the Fargate `sql-runner` image with `psql-run` /
  `mysql-run` and safe SQL templates, `jobset_tool.py`, `sync-jobs.sh`. Examples
  `examples/jobs/postgres-basic` and `examples/jobs/minimal`.
- Operator CLI `scripts/orch.py`: start (with `--wait` over the whole chain), status, stop,
  report, list, gate, post, dumps, restore. `scripts/verify_destroyed.py` lists leftovers after a
  destroy.
- Denylist gate `scripts/check_denylist.py`: denied terms in files, paths, commit messages and
  authors from a list kept outside the repository (`DENYLIST_PATTERNS`); output names only
  `path:line` and the pattern number; CI step, pre-commit and commit-msg hooks, `make denylist`.

**Sandbox and tests**
- `examples/sandbox`: VPC without NAT, two Aurora PostgreSQL Serverless v2 clusters with
  auto-pause, DMS, the orchestrator; `scripts/sandbox.py` and the e2e scenarios in
  `scripts/e2e.py` (matrix in `docs/testing.md`), among them `s1p` (after auto-pause), `idem-sfn` (a real `ExecutionAlreadyExists` from post's `StartResume`),
  `s10snap`, `s8fargate`, `s1iam`, `guard`, `postdie`, `s16`.
- The upstream aurora/dms modules as a configurable dependency (ADR 0010): `sbx fetch-upstream`
  checks out the repository and commit named in `UPSTREAM_MODULES_GIT` / `UPSTREAM_MODULES_REF`
  (environment or the gitignored `upstream.env`) and applies `upstream-patches/*.patch`
  if present; `make sandbox-validate` and a CI job behind the repository variable.
- Tests: pytest (logic, Lambdas with moto, state machine paths with a local JSONata runner,
  sql-runner, CLI, docs), `terraform test` with a mocked provider, AWS TestState cases generated
  by `tests/sfn/make_cases.py`.

**Documentation**
- `docs/getting-started.md`, `docs/troubleshooting.md`, `docs/testing.md`, `CONTRIBUTING.md`,
  `AGENTS.md` and `docs/ai-assisted-development.md`; `docs/configuration.md` generated from
  `variables.tf` and `outputs.tf` by terraform-docs (`make docs`, `make docs-check`); README,
  architecture, RUNBOOK, state machines, pre-flight, job contract with a tutorial, security model
  with the tests that enforce each control; ADR 0001–0010.
- `LICENSE` (MIT, dms-orchestration contributors), `SECURITY.md` (private vulnerability
  reporting), pull request template.

### Changed
- A target reset before `reload-target` truncates **all** tables (DMS reloads every table and the
  target uses `DO_NOTHING`); before `resume-processing` only partially loaded tables.
- Connections live in the Terraform task registration (secret ARNs allow-listed in
  `job_secret_arns`), not in the job set.
- **Breaking:** `fail_on_table_errors` defaults to `true`. DMS finishes a full load with tables
  in `Table error` without failing the task; the run now fails `DmsTableErrors`, `on_failure` jobs
  run, and `resume` reloads the errored tables. Opt out per task or with `failOnTableErrors: false`.
- `builtin:checks/connections` runs `TestConnection` on endpoints that are untested or not
  `successful` and waits for the result: `ModifyEndpoint` resets the state DMS needs to start.
- `TARGET#`/`TABLE#` keys are `<project>/<taskKey>`, so they survive a recreated DMS task.
- Serverless DMS (`aws_dms_replication_config`) is not supported: the module refuses a `null`
  replication instance with a message.

### Fixed
- Step Functions names a refused start `DatabaseMigration.InvalidResourceStateException`; a start refused that way fails at once with the DMS text (`DmsStartRejected`);
  untested endpoints are classified `CONFIG`.
- `StopReason` in the long form `Last Error ... Stop Reason <NAME> Error Level ...` is parsed; the DMS task log tail works and gives table errors their category; tables in `Table error` lead to `reload-target` with a full reset.
- The event handler re-describes for up to 120 s after a stop event: Describe lagged the event by
  more than 30 s on `reload-target`.
- `SaveContext` wrote `context.json` as a JSON string (the S3 integration encodes a string `Body`
  once more); the body is an object.
- `Arm` replaces a `posted` item left by a `post` that died, never an `armed` or `claimed` one; `release_lock` releases the lock of a `post` that ended FAILED, TIMED_OUT or ABORTED
  in any phase while the lock still names its run.
- `migrate` no longer touches the target before every refusing check passed; a failed assessment
  or `MigrationAlreadyArmed` used to leave it truncated.
- A successful restore records the restored target as the baseline (`source=restore`), and the next
  run never resumes after it: `MANUAL` `RestoredTarget`, a full reload with `allowReset`.
- `pg-restore-s3` drops the PostgreSQL 17 header setting `transaction_timeout` that Aurora 16
  refuses and reports psql's exit code.
- Fargate job task definitions get an init container that opens the scratch volumes to uid 10001
  (Fargate mounts them empty and root-owned); job-runner reads the exit code of the
  `job` container by name.
- The Data API built-ins wait for an auto-paused Aurora cluster, including
  `DatabaseUnavailableException` while a woken writer comes back.
- job-runner: DynamoDB SDK attribute type `Bool`; ECS `.sync` managed-rule permission
  without Fargate images; database security groups created in the same plan.
- TestState cases supply every variable and send a `context` only with a mock; the sandbox image build context lists every file the Dockerfile copies.
- `verify-target` on Fargate aggregates whole rows: a column named like the subquery alias (the
  sequences query) turned every row into a string; `sbx build-image` refuses a build
  source in S3 older than the working tree, the image tags being immutable.

### Security
- One job role per project (`<name>-<env>-job-<project>`) with only that project's secrets, Data
  API clusters, job sets and dumps; Fargate jobs run with it as task role, built-in Lambda jobs
  assume it; the shared job roles have no secret access.
- `psql-run --template` renders `{{ expr }}` as a SQL literal and `{{ ident:expr }}` as a checked
  identifier; there is no raw splicing.
- The job wrapper enforces the job timeout on commands that print nothing (exit code 124).
- Without a CMK the SNS topic is not encrypted with `alias/aws/sns`, which would silently drop
  CloudWatch alarms; with a CMK the key policy requirements list every module role.
- `include_execution_data` defaults to `false`; the jobs security group has no ingress and 443
  egress only to the VPC and the gateway prefix lists.

### Removed
- The callback wait: `task-runner.asl.json`, `register_waiter`, `src/shared/waiting.py`, `WAITER#`
  items, `.waitForTaskToken` and heartbeats, variables `heartbeat_seconds` and
  `resume_wait_seconds`, output `state_machine_arns.task_runner`, alarm `OrphanedWaiter`
  (replaced by the two machines, ADR 0005).
- `completion=full-load-complete` from the run request and `defaults.completion`: the first
  iteration is a full load; CDC and cutover belong to the database team.
