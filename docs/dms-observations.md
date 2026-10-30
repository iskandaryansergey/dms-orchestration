# Observed AWS behaviour

Appendix to [`troubleshooting.md`](troubleshooting.md): what AWS DMS and Step Functions really
do, recorded in the sandbox
(`examples/sandbox`: us-east-1, DMS engine 3.5.4 on `dms.t3.small`, RDS PostgreSQL 16
source and target, full-load task, `TargetTablePrepMode = DO_NOTHING`). Raw events were
captured by an extra EventBridge rule into a log group. Every item below changed code or
tests; the commit is named.

## 1. EventBridge events

Shape of a task state event (verbatim, ids shortened):

```json
{
  "detail-type": "DMS Replication Task State Change",
  "source": "aws.dms",
  "resources": ["arn:aws:dms:us-east-1:<account>:task:BMJIQF4K5NAQRALLQ3RVB2OXXU"],
  "detail": {
    "eventId": "DMS-EVENT-0079",
    "eventType": "REPLICATION_TASK_STOPPED",
    "detailMessage": "Stop Reason FULL_LOAD_ONLY_FINISHED",
    "type": "REPLICATION_TASK",
    "category": "StateChange"
  }
}
```

| eventType | eventId | detailMessage | When |
|---|---|---|---|
| `REPLICATION_TASK_STARTED` | DMS-EVENT-0069 | `Replication task started, with flag, fresh start` | ~7 s after StartReplicationTask |
| `REPLICATION_TASK_STOPPED` | DMS-EVENT-0079 | `Stop Reason FULL_LOAD_ONLY_FINISHED` | end of a full load |

* The task ARN is in `resources`, as the rule and the handler assume.
* Events reach the Lambda 10–15 s after their `time`.
* **Describe lags the event.** One second after `REPLICATION_TASK_STOPPED`,
  `DescribeReplicationTasks` still reported the task as running; the handler left the wait
  pending and the watchdog closed it 90 s later. Fix: re-describe up to 6 × 5 s on
  stop/fail events. In a later run Describe caught up only after ~30 s, i.e.
  at the edge of that budget; the watchdog remains the backstop. Describe stays the
  source of truth (ADR 0002). During a load Describe also kept showing the *previous*
  run's statistics, so test drivers must not time anything off `ReplicationTaskStats`.

## 2. DescribeReplicationTasks

* `StopReason` comes in two forms:
  `Stop Reason FULL_LOAD_ONLY_FINISHED` and, after a stop that raced the end of the load,
  `Last Error The task stopped abnormally Stop Reason FULL_LOAD_ONLY_FINISHED Error Level NO`.
  The parser now takes the name after `Stop Reason` anywhere in the text.
* Stopping a task right after its load finished left it `failed` with
  `Last Error The task stopped abnormally Stop Reason RECOVERABLE_ERROR Error Level FATAL`.
* `StopReplicationTask` is refused ("is currently not running", boto3
  `InvalidResourceStateFault`) for the whole ~30 s of a small full load: a short load cannot
  be interrupted from outside, which is why S2 interrupts with a table error instead.
* `ReplicationTaskStats` dates come back with the caller's local offset
  (`2026-10-27 16:30:57.551000+04:00`); comparisons use aware datetimes, fine.
* 235k rows (4 tables, 5k LOB rows ~10 KB) full-load in ~7 s on `dms.t3.small`;
  task start to `REPLICATION_TASK_STARTED` ~7 s; the whole S1 took 2.5 min (most of it the
  90 s watchdog delay before the fix).

## 3. Errors from StartReplicationTask

* Step Functions names the error **`DatabaseMigration.InvalidResourceStateException`**,
  not `...InvalidResourceStateFault` (the API reference name). The Catch never matched. boto3 calls the same error `InvalidResourceStateFault`.
* **A task does not start until every endpoint passed TestConnection.** Message:
  `Test connection for replication instance <ri> and endpoint <ep> should be successful
  for starting the replication task`. It was classified `TRANSIENT_SOURCE_CONN`; now
  `CONFIG`, not retryable. `ModifyEndpoint` resets the state too (S1,
  second attempt). **Agreed in a design review with the platform team:** the pre-job `builtin:checks/connections` runs
  TestConnection itself on endpoints that are untested or not `successful`.

## 4. Table errors (scenario S3)

* With `SELECT` revoked on one source table DMS **does not fail the task**: it ends
  `stopped`, `Stop Reason FULL_LOAD_ONLY_FINISHED`, `TablesErrored = 1`, the table in
  `Table error`, `LastFailureMessage` empty. With the default `failOnTableErrors = false`
  the run went on to post-jobs and only the row-count job caught it. **Agreed in a design review with the platform team:**
  `fail_on_table_errors` now defaults to `true` in our module; opt out per task or per request.
* The cause is only in the task log (`dms-tasks-<instance>`), e.g.
  `[SOURCE_UNLOAD ]E: RetCode: SQL_ERROR SqlState: 42501 ... permission denied for table transactions`
  and `[TASK_MANAGER ]W: Table 'app'.'transactions' was errored/suspended`.
* The log tail never worked: the filter `?E: ...` is invalid for FilterLogEvents
  (`Invalid character(s) in term ':'`), and without `startTime` the call scans the group from
  its start and returns an empty page with `nextToken`. Fixed;
  DmsTableErrors now gets its category from the log (PERMISSIONS here).
* **`resume-processing` does not reload a table in `Table error`** (it stayed `Table error`,
  0 rows, after the grant was restored). Interrupted tasks with errored tables now get
  `reload-target` with a full reset.

## 5. Step Functions and IAM (found by the first apply/run)

* `aws-sdk:dynamodb` integrations take AWS SDK member names: `"Bool"`, not `"BOOL"`.
  CreateStateMachine accepts the wrong name; every execution then fails at runtime
  (static test added).
* CreateStateMachine checks the managed rule of every `.sync` state: job-runner needs the
  `StepFunctionsGetEventsForECSTaskRule` permissions even without Fargate images.
* `for_each` keyed on security group ids fails when the database SG is created in the same
  plan.

## 6. Sandbox account limits

* Guardrail policy v2/v3: `rds:CreateDBCluster` was denied unless `rds:DatabaseClass` is
  `db.t3.micro | db.t4g.micro | db.serverless`; Aurora requests carry no class, so no
  Aurora cluster could be created (the first runs used RDS PostgreSQL `db.t4g.micro`). Policy v5
  and a paid account plan (`FreeTierRestrictionError` on the free plan) fixed it.
* DMS: the smallest orderable class in us-east-1 is `dms.t3.small` (no `t3.micro`).
* The `dms` interface endpoint exists only in us-east-1b/c/d.

## 7. Aurora Serverless v2 with the upstream modules

Stack: two Aurora PostgreSQL 16.8 Serverless v2 clusters (0-2 ACU, auto-pause) from the upstream
`aurora` module, DMS 3.5.4 `dms.t3.small` and a full-load task from the upstream `dms` module
(both pinned to one commit), the orchestrator wired to the module outputs. Scenarios S7, S1,
S1p, S2, S3, S5, S6 green; 16/16 AWS TestState cases.

* **The upstream `dms` module sends `MaxFileSize = 0` on a PostgreSQL target.** It renders
  `postgres_settings` for every PostgreSQL target without `max_file_size`, and provider
  5.100 sends 0 (in state as 0). DMS then keeps every table `Before load`, 0 rows, the
  instance at ~65 % CPU, the task `running` for as long as you wait (8 and 40 min). With
  the DMS default 32768 KB the same 235k rows load in ~10 s. Sandbox: `sandbox.py
  test-connections` raises it through `ModifyEndpoint`; fix proposed upstream
  (`docs/upstream-modules-contract.md` 2.6).
* **`ModifyEndpoint` resets the endpoint's connection test** and starts a new one by itself
  (an immediate `TestConnection` fails with `InvalidResourceStateFault` "Connection
  is already being tested"); a start before that test passes is refused. The orchestrator
  tests in its pre-check and waits for a running test.
* **Secrets created in the same plan break the upstream module's `for_each`** (`endpoints_with_secret`
  keyed on `secret_arn != ""`): plan fails with `Invalid for_each argument`; a second
  `destroy` after the secrets are gone fails the same way. Fixed upstream from 08def61
  (contract 2.3): a sandbox run on it applied without `-target` and destroyed in one pass
  (181 resources), so the README no longer has the targeted steps.
* **IAM eventual consistency** on the first full apply: a resource that assumes a role
  created seconds earlier is refused; the second `apply` succeeds without changes to the
  configuration. The sandbox runbook retries once. One cause was ordering, not
  propagation: VPC Lambdas and the job-runner machine were created before the inline
  policies of their roles (`CreateNetworkInterface`, managed rule of `ecs:runTask.sync`); they
  now `depends_on` those policies (§9.6).
* **The orchestrator refuses `reload-target` for a task that never ran** (`InvalidStartType`,
  DMS accepts only `start-replication` for it); the e2e asks `start-replication` on the
  first run and `reload-target` afterwards.
* **Data API**: the built-in SQL jobs (`source-grants`, `row-counts-data-api`,
  `drop-control-tables`) work through the `rds-data` interface endpoint with
  `enable_data_api = true` (the upstream default is false). `row_counts` no longer fails a dry
  run, where a mismatch is expected.
* **Auto-pause (S1p)**: both clusters paused ~9 min after the last S1 (seconds_until_auto_pause
  300 + scale-down); the next run woke them through the Data API
  (`DatabaseResumingException` retried) and DMS connected to the resumed clusters; S1p
  took 147 s against 127 s warm.
* **Load speed vs. StopReplicationTask**: Aurora took the 235k rows in ~12 s, while DMS
  accepted an external stop only 17 s later, after `FULL_LOAD_ONLY_FINISHED`. Interrupting
  a load reliably needs the target held (S2 keeps a `SHARE` lock on one table);
  DMS then needs ~5 min (its stop timeout) to give the task up.
* **AWS TestState vs. the local runner**: TestState does not supply
  `$states.context.Task.Token` for `.waitForTaskToken` states and fails on an undeclared
  variable; the local runner did both silently. The cases now give the token and declare
  every variable `ResolveContext` assigns.

## 8. Fargate `sql-runner`

* The image is built by CodeBuild in the sandbox (`examples/sandbox/sql_runner.tf`, base
  image from the ECR Public mirror: Docker Hub rate-limits CodeBuild), gated by trivy as in
  CI (`--severity HIGH,CRITICAL --ignore-unfixed`) before the push. The ECR basic scan cannot
  ignore unfixed CVEs: it reports 8 HIGH in Debian packages without a fix (util-linux,
  cyrus-sasl2, zlib), 0 CRITICAL; report only.
* **Fargate mounts task volumes empty, `root:root 0755`, without the image content**, even
  when the image creates the directories for uid 10001: the job container (user 10001,
  read-only root) failed on `mkdir /tmp/orch` (`PermissionError`). Fix: a
  non-essential init container of the same image runs as root (`chmod 1777 /tmp`, `chown
  10001 /work /out`) and exits; the job container `dependsOn` it with `SUCCESS`. job-runner
  reads the exit code of the container named `job` (DescribeTasks does not keep the order).
* **S1f green:** Step Functions -> ECS RunTask -> init container -> job
  container (uid 10001, read-only root) -> psql over TLS (`verify-full`, RDS CA bundle) to
  Aurora. Fresh task: `start-replication`, no reset by design, post `tls-target` ok
  (`pg_stat_ssl.ssl` true for the job's session), counts match, 239 s. Second run:
  `reload-target`, reset `truncate-tables` on Fargate ok (4 tables), load, post ok, 210 s.
  A Fargate job takes ~40-60 s end to end (ENI, image pull, two containers).
* ECS creates `/aws/ecs/containerinsights/<cluster>/performance` for Container Insights;
  it is not in Terraform state and survives `destroy` (1 day retention).

## 9. Two state machines, pre-flight verdicts and dumps, live

The whole e2e matrix on the upstream modules (same pin), us-east-1,
DMS 3.5.4 on `dms.t3.small`, Aurora PostgreSQL 16.8 Serverless v2.

### 9.1 `StartExecution` with a fixed name (`e2e.py idem`)

| Call | Answer |
|---|---|
| same name, same input, first execution still running | `200`, the **same** `executionArn` (idempotent) |
| same name, other input, first still running | `ExecutionAlreadyExists` |
| same name, same input, after the first **ended** | `ExecutionAlreadyExists` (idempotency holds only while it runs) |

So the handoff's rule stands: `ExecutionAlreadyExists` counts as "post already started", and the
claim in `MIGRATION#` stays the main guard (a repeat after the end is not a new post).

The table above comes from boto3. Inside a state machine the integration names the error
`StepFunctions.ExecutionAlreadyExistsException` (the Catch of `StartResume` in post and of
`StartPostDryRun` in migrate); a real call confirms that name (§9.5). `e2e.py idem-sfn`
makes the real call: post's `StartResume` meets a decoy execution `<runId>-r2` with another
input and must take the "already started" branch (`WriteReport`, `RESUMED`), not `GiveLockBack`
(live: §9.5).

### 9.2 Premigration assessment (`e2e.py s14`)

* **Task state:** started from a task in `stopped` (after a completed full load) — accepted. No
  `ready` needed.
* **Service-linked role:** none in the account (`AWSServiceRoleForDMSServerless` and
  `AWSServiceRoleForDMS` absent before and after); the run started and finished. In provisioned
  mode `StartReplicationTaskAssessmentRun` needs only the module's assessment role
  (`dms-orch-sbx-assessment`, trusted by `dms.amazonaws.com`, scoped to the bucket folder) and
  `iam:PassRole` on it. The upstream module's output lists `iam:CreateServiceLinkedRole` "unless the role
  exists" — not needed here, so an SCP denying it should not block a provisioned assessment
  (not tried under an SCP).
* **Run:** 15 individual assessments, ~3.5 min (`StartAssessment` 15:51:23 → last poll 15:54:55
  UTC), result `failed` (3 failed: `postgres-check-rds-roles`,
  `table-with-lob-but-without-primary-key-or-unique-constraint`, `postgres-check-pk-validity`;
  1 warning: `unsupported-data-types-in-source`). Report in the assessment bucket under
  `dms-assessments/core/`, `SSE_S3`. The run stopped `AssessmentFailed` before `Arm`; DMS not started.
* Note: the assessment ran after the reset jobs, so a refused assessment left the target
  truncated. Fixed (assessment before the reset); proven live in §9.4.

### 9.3 Other facts of the same run

* **Step Functions → S3 `putObject`:** a string `Body` (`$string({...})`) is JSON-encoded once
  more; the object holds a quoted JSON string. Pass the object itself.
* **Describe lag on `reload-target`:** `DescribeReplicationTasks` lagged
  `REPLICATION_TASK_STOPPED` by more than 30 s on both reload runs before the fix (post started by
  the watchdog ~1 min later); the handler now re-describes for up to 120 s; after it,
  posts came from `event-handler`.
* **PostgreSQL 17 client, Aurora 16 server:** `pg_restore` writes `\restrict <key>` and
  `SET transaction_timeout = 0;` in the SQL header; Aurora 16 refuses the parameter. The restore
  drops that header line. `psql --single-transaction` does not wrap a script read from stdin,
  so the restore sends `BEGIN;` first and `COMMIT;` only after `pg_restore` exited 0
  (`ROLLBACK;` otherwise).
* **`terraform apply` reset the target's `MaxFileSize` to 0** with upstream dms modules before
  commit `08def61` (contract 2.6), and with it the connection status; from that commit on an
  apply keeps the setting. The orchestrator still refuses a PostgreSQL target endpoint with
  `MaxFileSize = 0` before the start (outside `plan`), as a safety check.
* **TestState** against AWS: 25/25 cases (AWS refuses a `context` without a mock). The
  error name `StepFunctions.ExecutionAlreadyExistsException` is only mocked in those cases, not
  seen from a real call.
* **Dump:** 235k rows, 4 tables → `target.dump` 3.2 MB in 2.9 s on Fargate (stream, no disk).

### 9.4 Live proof of the fixes

* **Assessment before the reset:** S14 (`reload-target`, `targetReset=always`,
  `failOnWarning`) → `FAILED AssessmentFailed` (15 checks: 3 failed, 1 warning, as in §9.2);
  target counts before = after (10000 / 20000 / 200000 / 5000). `e2e.py guard`: a planted `armed`
  `MIGRATION#` → `FAILED MigrationAlreadyArmed`, no `JOB#dump#`/`JOB#reset#`, target untouched,
  DMS not started.
* **Restore baseline:** S10 → `TARGET#BASELINE` `source=restore`, `runId`
  = the run that took the dump, `restoreRunId`, `dumpUri`; the next `plan` → `manual`
  `RestoredTarget`; S4 (`allowReset`) after it → verdict `reset`, `reload-target`, counts =
  source, gate passes.
* **Lock of a dead post:** `e2e.py postdie` — post fails in `LoadRun`; its own lock
  gone 0.2 s after the execution ended (the watchdog grace is 300 s); a lock handed over to
  `<runId>-r2` stays. S13 (repeat post) unchanged.
* **Fan-out:** S16 — the FAILED and MANUAL notifications reached the queue with
  `severity=high`, SUCCEEDED did not. The queue also holds the notifications of earlier failing
  runs of the session; `e2e.py s16` drains them first (5 here).
* **TestState** against AWS: 29/29 (new states `ReadMigration`, `ArmGuard`).
* Both S1 posts started by `event-handler` (Describe lag fix holds). Apply: 176
  resources on the first full pass, no IAM eventual-consistency retry this time.

### 9.5 Snapshot dumps, Fargate check, IAM login, auto-pause, live

The full stack with every optional part (`sql_runner`, `assessment`, `sqs_fanout`,
`snapshot_dumps`, `iam_auth`), upstream modules fetched by `sbx fetch-upstream`.

* **Apply:** 186 resources on the first full pass, `iam_auth = true` included (the aurora module
  creates `orch_iam` through the Data API with the separate admin secret); no IAM
  eventual-consistency retry.
* **Auto-pause (`e2e.py s1p`):** both clusters reported 0 ACU 612 s after the last load; the woken
  run's first pre-flight check finished 51 s after the start, the whole run 164 s
  (`SUCCEEDED`, counts match). The Data API retries covered the wake-up.
* **`StepFunctions.ExecutionAlreadyExistsException` from a real call (`e2e.py idem-sfn`):** post's
  `StartResume` met the decoy `<runId>-r2` with another input; the error name inside the state
  machine is exactly that; the Catch took the "already started" branch (`WriteReport`, `RESUMED`),
  no `GiveLockBack`, decoy untouched.
* **Cluster snapshot (`e2e.py s10snap`):** `CreateDBClusterSnapshot` with the project, task and
  run tags under the conditions of the job role (`aws:RequestTag`, `rds:cluster-snapshot-tag`)
  and `DescribeDBClusterSnapshots` filtered by the cluster: allowed. Snapshot
  `<project>-<taskKey>-<ts>` available ~3 min after the request (235k rows, `AllocatedStorage`
  reported as 0 for Aurora), `dump.json` written, the retention job ran; the scenario deletes the
  snapshot.
* **Fargate target check (`e2e.py s8fargate`):** the first image failed in `fingerprint.build` on
  the sequences query: the psql wrapper aggregated `json_agg(q)` over a subquery aliased `q`, and
  PostgreSQL resolved `q` to the column `sequencename AS q`, so each row came back as a string
  (checked through the Data API with both wrappers). Fixed (qualified whole row
  under its own alias). After it: the same fingerprint hash through the Data API and psql, then
  `MANUAL` `TargetChanged` with `app.junk` extra and `app.accounts` in `rowsChanged`. The
  scenario leaves the target spoiled; the next run needs `s4` (`allowReset`).
* **Sandbox image build:** CodeBuild builds from the source zip that the last `terraform apply`
  uploaded; a build right after a commit published the old code under the new, immutable tag.
  `sbx build-image` now refuses a zip that differs from the working tree.
* **IAM token login (`e2e.py s1iam`):** the Fargate post job `iam-target` logged in as `orch_iam`
  with a token, `current_user` and `pg_stat_ssl.ssl` checked in the session; the registration has
  `auth = iam` and no secret; the job spec and the job log group hold neither a password nor a
  signed token (`X-Amz-Signature`, `X-Amz-Credential`, `PGPASSWORD`, `password`: 0 events).
* **Regression on the final image:** S10 (restore baseline), S4, S14, guard, S16 (9 stale
  notifications drained first), S13, postdie (own lock gone 0.1 s after the end) and the final S1
  green; both S1 posts started by `event-handler`. TestState against AWS: 29/29.

### 9.6 0.2.0 on the SSM runtime, live

The stack with `ssm_runner`, `snapshot_dumps`, `assessment`, `sqs_fanout`, no image, upstream
08def61.

* **Apply and destroy:** one `terraform apply` without `-target`; the first pass failed on
  ordering (a VPC Lambda and the job-runner machine created before their role policies), the
  second completed. Destroy: 181 resources in one pass, `verify_destroyed` empty.
* **Validation default:** with `fail_on_validation_errors = true` a load of a table without a
  primary key ends `FAILED` `DmsValidationErrors` ("validation state No primary Key"): DMS cannot
  validate such a table. The sandbox task turned the flag off for that reason; since then the
  state is reported (`validationSkippedTables`), not failed. A row changed by a
  trigger on the target (`e2e.py s21-validation`): `DmsValidationErrors` naming the table with
  the flag on, `COMPLETED_WITH_VALIDATION_ERRORS` with it off.
* **Pinned context:** reading `context.json` by `VersionId` needs `s3:GetObjectVersion`; without
  it every post stopped `ContextNotPinned` (AccessDenied).
* **SSM runner:** psql needs the RDS CA bundle; an AMI has none at the image's path. The
  runtime zip carries the pinned bundle and orch-job points the clients at it.
* **TestState:** AWS refuses a mock on a `Choice` and validates mocked results against the API
  schema (an SSM `CommandId` has 36 characters). A `States.Timeout` was retried by the
  `States.TaskFailed` retrier of `StartReplicationTask`; an explicit `States.Timeout` retrier
  with `MaxAttempts 0` comes first now. 47/47 against AWS after the fixes.
* **604 tables (`e2e.py s4big`):** the reset list went to `inspect/reset-tables.json` (604), the
  Lambda SQL runtime truncated all of them in one statement, DMS loaded them in about 90 s, and
  DMS validation was still running after 40 minutes. After such a run is stopped and the extra
  tables are dropped on both sides, the next run takes its reset list (604) from the DMS table
  statistics of the stopped run and its truncate fails on the missing tables (fail-closed).
* **Bucket controls:** a dump object written without SSE-KMS is denied (`DenyDumpsWithoutKms`),
  also for the operator.
