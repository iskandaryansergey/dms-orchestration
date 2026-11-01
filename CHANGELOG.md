# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions: SemVer (0.x until the
first production run). Commit references name the commit that made or fixed the item; the
observed AWS behaviour behind the fixes is in [docs/dms-observations.md](docs/dms-observations.md).

## [0.2.0] — 2026-10-30

Hardening of 0.1.0 and new job runtimes. Upgrading needs care: every breaking change is marked
under *Changed*; finish or abandon runs started by 0.1.0 before the upgrade.

### Security
- Run artifacts per project: `runs/<project>/<runId>/` (`context.json`, reports, `jobs/`). A
  project's job role reads and writes only `runs/<project>/*/jobs/*`; the shared `ecs-job` and
  `lambda-jobs` roles lose `runs/*`.
- `post` loads exactly the `context.json` version `migrate` saved: `migrate` records the S3
  version id on the run item (`CheckContextVersion`, `RecordContextVersion`) and `post` refuses
  any other version (`ContextNotPinned`).
- The operator policy no longer publishes job sets; publishing is a separate policy per project
  that only creates objects, so a published version is never overwritten.
- Job sets are pinned by hash: `bundle.sha256` (sorted file hashes plus a total) is checked
  against the registered total by `resolve_context`, the SQL and command runtimes and the
  built-in jobs; a missing hash fails `JobSetNotPinned`, any difference `JobSetTampered`, before
  a connection is resolved.
- A job result holds no command output: a failed job names its exit code and CloudWatch log
  stream. A dry run opens read-only database sessions (`default_transaction_read_only` for libpq
  clients and the Lambda SQL runtime; advisory for scripts with other drivers).
- Chain fields of a run request (`attempt`, `parentRunId`, `parentStartedAt`) are accepted only
  from the execution `post` handed the lock to (`RUN#<parent>.resumeChildRunId`), else
  `InvalidRunRequest`.
- Snapshot rights are bounded by the project tag, not the identifier prefix: create only with the
  request tag `dms-orch:project = <project>`, tagging only of a snapshot already tagged with the
  project or not tagged yet; no project name may be a prefix of another.
- Restore runs under the task lock, checks the manifest's project and task before downloading,
  restores only the manifest's tables and commits only after `pg_restore` succeeded.
- The job-runner no longer trusts its input (the operator may start it for `orch.py restore`):
  `prepare_job` runs a job only if it is in the job set version recorded on the run's `RUN#` with
  the same code (looked up again by name, bundle hash from the registration, not from the input)
  or is one of the module's own jobs with its fixed code (snapshot retention at least
  `dump_retention_days`), for a `RUNNING` run of the project and task named. The job context is
  built from the registration and `RUN#` only: `inspect_task` records the run's decision (start
  type, reset scope and list, migration type, selection rules) on `RUN#`, and a reset list in S3
  is read only under the run's `inspect/` prefix. The restore phase accepts only `pg-restore-s3`
  of that task's dumps and the restore baseline check, under the task lock held by that restore
  execution (the job-runner passes its execution id to `PrepareJob`). `dryRun` is the one on
  `RUN#`: an input that differs is refused before anything is written, and the job-runner runs and
  records the job with the flag `PrepareJob` returned, so a dry re-run of a real run's job can no
  longer overwrite its `JOB#` record with `ok` and close a failed post check (run 11, finding 2);
  migrate and post pass the run's flag to every job, the baseline jobs included. Anything else
  fails `JobNotInJobSet`. The operator policy denies reading `runs/*/jobs/*`, so a
  job artifact named `report.json` is not readable either; job results remain visible in the
  execution history.
- No reset without a target fingerprint; the reset scope comes from the fingerprint and the
  task's selection rules, not from the DMS table statistics.
- The optional `sql-runner` image is built from a digest-pinned base, checksummed downloads and
  pip wheels by hash, with mirror build arguments; PowerShell is off by default.
- The Lambda SQL runtime vendors `pg8000` by hash (`make vendor-check`) and connects with TLS
  `verify-full` against a pinned RDS CA bundle.
- SSM runtime: runners assume the project's job role through a short-lived profile, the
  document checks the sha256 of the runtime zip, connection files are `0600` and removed on
  every exit. Residual risk: projects that share one runner are not isolated from each other
  (sessions on the runner and other projects' jobs can see job files during a run); use
  one runner per project ([ADR 0011](docs/adr/0011-job-runtimes.md),
  [security.md](docs/security.md)).

### Changed
- **Breaking:** `EXTERNAL_STOP` is refused as an auto-resume category, in the run request
  (`autoResume.onlyCategories`, a validation error naming it) and in the registration
  (`defaults.auto_resume_categories`, a plan-time error): someone stopped the task on purpose,
  and the category is not retryable, so listing it only looked like it would resume. The sandbox
  scenario `s19-chain-dump`, which relied on it and never reproduced live, is replaced by an
  offline test of the chain retry → dump → restore ([testing.md](docs/testing.md)).
- **Breaking:** runs saved by 0.1.0 are refused by `post` and `orch.py post` (`ContextNotPinned`:
  the run item has no context version and the artifacts moved under the project). Bucket
  `versioning` is now required: without a version id `migrate` stops before DMS.
- **Breaking:** dumps made by 0.1.0 (a manifest without `tables`) are refused by `orch.py restore`
  and `pg-restore-s3`; `pg_restore` is limited to the manifest's tables (no whole-schema
  `--clean`) and the stream is wrapped in `BEGIN;` … `COMMIT;`.
- **Breaking:** a `partitioned` table in the scope of the task makes `pg-dump-s3` fail (`pg_dump -t`
  would take it without its partitions); use `dump.method = snapshot` for such targets.
- **Breaking:** a run uses a job set version only when it is pinned in `jobs.bundles` of the task
  (`scripts/jobset_tool.py hash`, printed by `sync-jobs.sh`), unless `jobs.allow_unpinned = true`
  (sandbox, development).
- **Breaking:** `sync-jobs.sh --force` is gone: uploads are create-only (`--if-none-match`),
  `bundle.sha256` last; publish a new version instead.
- **Breaking:** the operator policy is split: `operator_policy_json` no longer uploads job sets and
  reads only run reports; attach `jobset_publisher_policy_json["<project>"]` to whoever publishes
  that project's job sets. The operator policy gains `PutItem`/`DeleteItem` on `LOCK#` items for
  a restore under the task lock.
- **Breaking:** `failOnValidationErrors` defaults to `true`
  (`defaults.fail_on_validation_errors`): a table DMS did not validate, failed validation records
  or full-load error rows fail the run `DmsValidationErrors`; off, it succeeds as
  `COMPLETED_WITH_VALIDATION_ERRORS`. A table DMS cannot validate (`No primary key`) never
  fails the run: the report lists it in `validationSkippedTables` and its warnings.
- **Breaking:** Fargate is optional: without `fargate.images` the module creates no ECS cluster,
  task definitions, ECS roles or ECR endpoints, `ecs_cluster_arn` is `null` and job roles do not
  trust ECS tasks. SQL jobs run in a Lambda, long jobs on SSM.
- **Breaking:** registry conventions: every resource is gated on `create`; every input is
  `nullable = false`; unset optional strings are `""` instead of `null` (`kms_key_arn`,
  `permissions_boundary_arn`, `artifacts_bucket_name`, `fargate.cluster_arn`,
  `dms.log_group_name`, connection fields, `lambda.artifact_s3_*`; `assessment.role_arn = ""`
  turns the step off); `lambda.reserved_concurrency` defaults to `-1`. The `null` sentinels of
  the HTTPS egress are replaced by the booleans `https_egress_vpc_cidr` and
  `https_egress_gateway_endpoints` (the lists only add ranges). Tags gain `ManagedBy` and
  `Module`; the `dms-orch:*` tags stay.
- **Breaking:** `kms_key_policy_requirements` follows the KMS key policy contract v2 (`sid`,
  `principals {services, arns, roles}`, `actions`, `conditions`, `purpose`; symbolic roles
  `deployer` and `module`); the v1 statement list is renamed `kms_key_policy_statements`.
- **Breaking:** the Python sources moved from `src/` to `modules/dms-orchestration/src`: the module
  packages only from its own directory. Prebuilt Lambda zips and tooling that point at `src/`
  need the new path.
- **Breaking:** the minimum commit of the upstream aurora/dms modules for the sandbox is
  `08def61`: from it an apply keeps the target's `MaxFileSize` and the sandbox needs no
  targeted apply or destroy. The start check that refuses `MaxFileSize = 0` stays.
- `RecheckTask` reads the DMS task again right before the dump and before the reset; a task that
  runs, is gone or changed stops the run (`TaskStateChanged`) before the target is touched.
- `post` stops when it cannot take over the lock: `LockLost`, report `MANUAL`, no jobs, the other
  run's lock left alone.
- `StartReplicationTask` is not retried on a timeout (the start may have reached DMS); the
  handoff judges the task.
- A chained resume dumps before its reset unless a run of the chain already dumped
  (`RecordChainDump`).
- Pre-flight without a fingerprint: `allowReset` never resets (`manual` / `NoFingerprint`), and a
  fresh task is `manual` unless `preflight.allowNoFingerprint`. Above 500 tables the reset list
  goes to `inspect/reset-tables.json` and is expanded into the job spec only; `psql-run
  --truncate-scope` truncates it.
- The pre-flight target check runs in the Lambda through `pg8000` when the target connection has
  a `host`, so no image is needed for it.
- `orch.py restore` holds the task lock (`LOCK#`) for its whole duration and takes
  `--runtime ssm|fargate` (SSM by default when the module has it); `orch.py report` takes
  `--project` or reads it from the run item.

### Added
- Lambda SQL runtime: `sql` jobs (connection, file or inline SQL, vars, `singleTransaction`,
  `truncateScope`) in the `sql_jobs` Lambda (`lambda.sql_jobs_enabled`, on by default); the
  built-in SQL jobs use `pg8000` for connections with a `host` and the Data API otherwise. The
  example job sets run every job in a Lambda.
- SSM Run Command runtime for long jobs on instances built from your AMI (`ssm`): the
  `job-runner` branch `FindSsmRunner` → `RunSsmJob` → `WaitSsm`/`GetSsmInvocation`, the module's
  Command document and runtime zip, outputs `ssm_runner_policy_json`, `ssm_document_content`,
  `ssm_document_name`; `pg-dump` dumps run there when it is on. RUNBOOK §16.
- Output `bucket_requirements` (versioning, SSE-KMS default, TLS only, `DenyDumpsWithoutKms`,
  lifecycle) and the module `modules/artifacts-bucket`, which creates a bucket with those
  controls; `network_contract` lists `rds` with snapshot dumps and `ec2messages`/`ssmmessages`
  with the SSM runtime.
- Output `jobset_publisher_policy_json` (one policy per project); `scripts/jobset_tool.py hash`.
- [ADR 0011](docs/adr/0011-job-runtimes.md): Lambda for SQL, SSM for long jobs, Fargate optional,
  the module never builds an image.
- Tests: TestState cases `post-lock-lost-stops`, `recheck-task-*`,
  `dump-runs-on-chained-reset-without-dump`, `start-timeout-is-not-retried`, `job-ssm-*`;
  `terraform test` for the per-project roles, the SSM runtime, Fargate optional, `create = false`
  and the KMS contract v2; `test_packaging.py`; e2e scenario `s1ssm`, and `s10` on the SSM
  runner. Live scenarios still to add are listed in [docs/testing.md](docs/testing.md#planned-scenarios).

### Removed
- `sync-jobs.sh --force`.
- Advisory check `no_fargate_images`: SQL jobs no longer need an image.
- `pg-restore-s3 --schemas`: the manifest's tables are the restore scope.
- The command output tail in job results.
- The sandbox `MaxFileSize` workaround (`fix_target_max_file_size` in `sbx test-connections`) and
  the targeted apply and destroy steps: upstream fixed them in `08def61`.

### Fixed
- `post` may read the pinned `context.json`: the core Lambdas get `s3:GetObjectVersion`; without
  it every post stopped `ContextNotPinned` (e635341).
- The SSM runtime zip carries the pinned RDS CA bundle and `orch-job` points psql and mysql at
  it; a runner AMI has none at the image's path (4ad8649).
- Lambda functions and state machines depend on their role policies: a first apply no longer
  creates a VPC function or a `.sync` state before its policy (b33aa9f).
- A timed-out `StartReplicationTask` is not retried: an explicit `States.Timeout` retrier with
  `MaxAttempts 0` comes before the `States.TaskFailed` one (5e0fcc4).
- `truncateScope` and `psql-run --truncate-scope` (PostgreSQL) skip tables missing on the target
  and name them (`skippedMissing`, a NOTICE): a reset list from the DMS statistics of an earlier
  run no longer fails on tables dropped since (941d281, 5a8c2ed).
- A stopped task whose `StartDate` is after the start request stays pending while
  `DescribeReplicationTasks` still shows the previous run's `StopDate`; a load longer than the
  900 s grace no longer ends `DmsTaskNotStarted` on such a lag (3e79b81).
- Sandbox: the SQL Lambda reaches S3 through the gateway endpoint (25af970); interface endpoint
  keys come from the sandbox variables, so `terraform validate` has no cycle (aa19e10); the task
  turned `fail_on_validation_errors` off for its table without a primary key (e53fcec), no longer
  needed; the README applies and destroys without `-target` (544d1e8).

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
