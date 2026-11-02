# RUNBOOK — operating DMS orchestration runs

For operators of a migration environment (dev / stage / qa). Commands use
`scripts/orch.py` with `ORCH_STATE_MACHINE_ARN` (`state_machine_arns.migrate`), `ORCH_TABLE`,
`ORCH_BUCKET` set from the Terraform outputs, and the operator role (`operator_policy_json`).

A run is two executions (ADR 0005): `migrate <runId>` ends right after the DMS start
(status `STARTED`); the DMS event starts `post <runId>-post`, which runs the post-jobs or the
failure branch, reports and releases the lock. An auto-resume continues as `migrate <runId>-r2`.
`orch.py start --wait` and `status` follow the whole chain.

First deployment and first run: [docs/getting-started.md](docs/getting-started.md). Symptoms
from AWS that are not a run status (a load stuck in `Before load`, a refused start after an
apply, a slow destroy): [docs/troubleshooting.md](docs/troubleshooting.md).

## 1. Before a run

1. The DMS task exists and is **not running** (`start = false` in the DMS workspace).
   Do not run `terraform apply` in the DMS workspace during a migration: the upstream dms module
   stops a running task when it changes it (its "apply freeze"). Lock the HCP
   Terraform workspace for the duration of the run.
2. DMS refuses `StartReplicationTask` until every endpoint has passed **TestConnection**
   against the replication instance, and a `ModifyEndpoint` resets that state
   (`docs/dms-observations.md`). The orchestrator handles it: the pre-job
   `builtin:checks/connections` tests endpoints that are untested or not `successful`
   and fails the run before DMS starts if a test fails (`with: { test: false }` turns
   that off; a dry run only reports `wouldTest`). Put it first in `pre` of every job set.
   The upstream dms module does not test connections.
3. The job set version is uploaded: `scripts/sync-jobs.sh <project> <version> <dir>` (by a
   principal with that project's `jobset_publisher_policy_json`, not the operator role).
4. Check what the run will do:
   ```bash
   python scripts/orch.py start --project ledger --task core --operation plan --wait
   python scripts/orch.py report <runId>          # decision, start type, tables to reset, warnings
   ```
5. Optional rehearsal without touching DMS: `--dry-run` (jobs run with `ORCH_DRY_RUN=1`).
6. Pre-flight checks the target first ([docs/preflight.md](docs/preflight.md)): a target that
   changed since the last run, rows in the scope of a new task, or a completed task stop the
   run as `MANUAL` with the diff. Resetting it needs `--allow-reset`; nothing is reset silently.
7. Before promoting to the next environment: `python scripts/orch.py gate <runId>` (exit 0 only
   for a `SUCCEEDED` run whose baseline is still the latest).
8. Fargate jobs that log in with an IAM token instead of a secret: §13. Where the target check
   of pre-flight runs (Lambda or Fargate): §14. Aurora Serverless v2 with auto-pause: §15.

## 2. Start and watch

```bash
python scripts/orch.py start --project ledger --task core --change-ref CHG123 --wait
python scripts/orch.py status <runId>           # migrate + post executions + run item
python scripts/orch.py list --project ledger    # recent runs
```

Notifications go to the SNS topic: `SUCCEEDED`, `FAILED`, `REJECTED`, `RunAborted` from the
safety net, and `STARTED` only with `notify.onStart`. Between the two executions (DMS running)
the run item says `status=STARTED, phase=dms`. The report is in
`s3://<bucket>/runs/<project>/<runId>/report.md|json` (`orch.py report <runId> --project <p>`). The Step Functions console shows each
state; jobs are nested `job-runner` executions.

A run against an auto-paused Aurora Serverless v2 cluster starts slower: §15.

## 3. Outcomes

| Run status | Meaning | Next step |
|---|---|---|
| `SUCCEEDED`, outcome `COMPLETED` | full load done, post-jobs ok | check the report (row counts) |
| `FAILED` `DmsTableErrors` | load finished, some tables in error (default `failOnTableErrors=true`); `on_failure` jobs ran, post-jobs did not | DMS does **not** fail the task for a table error (the upstream dms module's `SUSPEND_TABLE`); the category and cause come from the task log (report). Fix it and run `--operation resume` (errored tables force `reload-target` with a full reset) |
| `SUCCEEDED`, `COMPLETED_WITH_TABLE_ERRORS` | load done, some tables in error; only with `fail_on_table_errors = false` on the task or `failOnTableErrors: false` in the request | see `tables` in the report; DMS does **not** fail the task for a table error, the cause is in the task log; fix it and run `--operation resume` (errored tables force `reload-target` with a full reset) |
| `FAILED` `DmsValidationErrors` | load finished, DMS validation found mismatches (`ValidationState` not `Validated`, `ValidationFailedRecords` or `FullLoadErrorRows` above zero; default `failOnValidationErrors=true`) | see `tables` in the report for the validation state per table; compare the rows, fix the cause and run `--operation resume`. A table without a primary key (`No primary key`: DMS cannot validate it) does not fail the run: it is listed under `validationSkippedTables` and in the report warnings, check it another way |
| `SUCCEEDED`, `COMPLETED_WITH_VALIDATION_ERRORS` | load done, validation mismatches; only with `fail_on_validation_errors = false` on the task or `failOnValidationErrors: false` in the request | see `tables` in the report; decide whether the mismatches are acceptable |
| `SUCCEEDED`, `STOPPED_AT_CHECKPOINT` | full-load-and-cdc stopped after full load (cached changes) | post-jobs done; continue with `--operation resume` |
| `SUCCEEDED`, `DRY_RUN` | rehearsal only (migrate hands the dry run to post without DMS) | — |
| post `RESUMED` | transient DMS failure; a new migrate `<runId>-r<n>` resumes the task with the lock handed over | follow it: `orch.py status <runId>-r<n>` |
| `FAILED` `MigrationAlreadyArmed` | a previous run still waits for its DMS event (`MIGRATION#` item) | `sandbox.py migration` / §6; normally the watchdog hands it to post within 5 min |
| `FAILED` `ContextNotPinned` | migrate: the artifacts bucket returned no object version for `context.json`, DMS was not started. post: `RUN#` has no `contextVersionId` or that version is gone (e.g. a run saved before this check) | enable versioning on the artifacts bucket and rerun; post does not load an unpinned context |
| `FAILED` `DmsStartRejected` | the start answered InvalidResourceState and the task never moved | read the DMS text in the report; fix the task state; rerun |
| `MANUAL` `RestoredTarget` | the target was restored from a dump (`orch.py restore`) and DMS still reports loaded tables: a resume would skip them | `--allow-reset` (full `reload-target`), see section 11 |
| `MANUAL` `TargetChanged` | the target differs from the baseline of the last run (diff in the report: extra / changed / rowsChanged) | find who changed it; undo, or run again with `--allow-reset` (truncates the scope, `reload-target`) |
| `MANUAL` `TargetNotEmpty` | the DMS task never ran (or was recreated) but the target has rows or earlier table states | confirm the rows are not needed; `--allow-reset` |
| `MANUAL` `AlreadyCompleted` | the task already loaded everything | `--allow-reset` to load again |
| `MANUAL` `PreflightTablesMissing` | pre-jobs keep creating tables but some are still missing after `preflight.maxIterations` | check the DDL pre-jobs |
| `MANUAL` `NoFingerprint` | no target check ran (no Data API connection `target`, no sql-runner image): `--allow-reset` or a first run would act on an unchecked target | give the target a check; for a first run only, `preflight.allowNoFingerprint: true` in the request (never resets), see `docs/preflight.md` |
| `MANUAL` `ResetNotConfigured` | `--allow-reset` but the job set has no `reset` phase | add a reset job |
| `FAILED` `RunAlreadyActive` | another run holds the lock | wait, or see §5 |
| `FAILED` `TaskAlreadyRunning` / `TaskBusy` | DMS task running/modifying outside the orchestrator | find who started it; stop it or wait |
| `FAILED` `AlreadyCompleted` | the task already finished its load | to load again: `--start-type reload-target` (truncates all tables first) |
| `FAILED` `ResetNotConfigured` | partial tables must be emptied but the job set has no `reset` phase | add a reset job, or `--reset-policy never` if duplicates are impossible |
| `FAILED` other | see `classification.category` in the report | §4 |

## 4. Failure categories

| Category | Typical cause | Auto-resume | Action |
|---|---|---|---|
| `TRANSIENT_SOURCE_CONN` / `TRANSIENT_TARGET_CONN` | network blip, failover | yes (default 1 time) | if it still failed: check endpoints, SGs, `checks/connections`; rerun |
| `DMS_RESTART` | DMS recycled/reconfigured the task | yes | rerun |
| `PERMISSIONS` | missing grant, wrong password, pg_hba | no | fix grants/secret; rerun (resumes, resets partial tables) |
| `DATA` | duplicate key, type/constraint errors, tables in error | no | fix data/DDL; rerun with reset |
| `RESOURCE` | low memory/disk on the replication instance | no | scale the instance (the upstream dms module); rerun |
| `EXTERNAL_STOP` | someone stopped the task in the console/CLI | no | confirm intent; rerun to resume |
| `JOB_FAILED` | a pre/post/reset job failed | no | see the job in the report (`jobs`, artifacts); fix; rerun |
| `TASK_DELETED`, `CONFIG`, `UNKNOWN` | — | no | read `lastFailureMessage` and `logTail` in the report |

**Rerun after a failure:** start the same command again. The run inspects the task
(now `interrupted`), truncates partially loaded tables (`if-interrupted`) and resumes —
completed tables are not loaded twice. Pre-jobs run again; they are idempotent.

## 5. Stop a run

```bash
python scripts/orch.py stop <runId>                       # stop the orchestration only
python scripts/orch.py stop <runId> --stop-task --reason "change window over"   # also StopReplicationTask
```

The task lock is released within seconds by the `release_lock` safety net (EventBridge
on ABORTED), at the latest by the watchdog (5 min). A later run sees the task as
`interrupted` and resumes it.

## 6. Stuck or orphaned state (rare)

| Symptom | Check | Fix |
|---|---|---|
| `RunAlreadyActive` but no run is going | `aws dynamodb get-item --table-name <table> --key '{"pk":{"S":"LOCK#<taskArn>"},"sk":{"S":"LOCK"}}'` → `executionArn`; `aws stepfunctions describe-execution` | if the execution is not RUNNING and the watchdog alarm fired, delete the item (same key) |
| DMS finished but no post execution | `orch.py status` (post `NOT_STARTED`); `MIGRATION#<taskArn>` item | the watchdog starts post within 5 min (a stale claim after 2 min, same name); if not, check alarm `watchdog-not-running` and the watchdog Lambda logs |
| Alarm `migrationoverdue` | DMS runs longer than the task's `runTimeoutSeconds` | the run goes on; check DMS progress; `orch.py stop <runId> --stop-task` ends it (post reports `EXTERNAL_STOP`) |
| Messages in `<prefix>-events-dlq` | alarm `events-dlq` | inspect the message (DMS event or execution status); the watchdog covers both; purge after review |
| Alarm `orphanedlock` | the watchdog cleaned up after an execution that died without its own cleanup | read the execution history; no action unless it repeats |

## 7. Alarms

| Alarm | Meaning |
|---|---|
| `<prefix>-migrate-failed / -timed-out / -aborted`, `<prefix>-post-…` | a run ended badly (also sent as a run notification) |
| `<prefix>-<lambda>-errors` | Lambda errors in the event handler, watchdog or release_lock |
| `<prefix>-events-dlq` | undelivered DMS/execution events |
| `<prefix>-watchdog-not-running` | no watchdog invocation for 3 intervals: lost events would not be recovered |
| `<prefix>-migrationoverdue` | a DMS run is older than its `runTimeoutSeconds` |
| `<prefix>-orphanedlock` | safety nets had to clean up |

## 8. Cutover and CDC

Not ours (design review with the platform team): the first iteration is a full load; CDC until steady state and cutover
belong to the database team. For full-load-and-cdc tasks configured to stop after the full
load, the run ends `STOPPED_AT_CHECKPOINT` after post-jobs. Application cutover stays with the application team and the platform
runbook ("point of no return").

## 9. Notifications and fan-out

Outside the sandbox every notification goes to the platform SNS topic (`sns_topic_arn`; the
`own_sns_topic_outside_sandbox` check warns otherwise). Each message carries SNS message
attributes `environment`, `project`, `taskKey`, `status`, `phase`, `severity`
(`high` for `FAILED`, `MANUAL`, `ABORTED`; `medium` for `REJECTED`; `low` otherwise), so a
subscription filters without parsing, e.g. on-call:

```json
{"severity": ["high"], "environment": ["prod", "stage"]}
```

CloudWatch alarms publish to the same topic without these attributes; a subscription that wants
them too adds a second subscription without a filter policy, or filters on the alarm text.

## 10. State outside the DMS workspace, destroy

The journal (DynamoDB: locks, runs, baselines, table copies; S3: job sets, reports, dumps) must
outlive the DMS workspace. On the target platform it lives in a long-lived
workspace and comes in through `table_name` and `artifacts_bucket_name` (check
`state_in_the_dms_workspace` warns otherwise). The sandbox keeps everything in one workspace.

After `terraform destroy` of a sandbox, list what is left:

```bash
uv run python scripts/verify_destroyed.py --match <name prefix> --profile <profile> --region us-east-1
```

Exit 0 means nothing matched. Container Insights is off by default (`container_insights`); its
log group used to survive destroy.

## 11. Dump before a reset, restore

With `dump.enabled` in the request (or `dump_before_reset = true` in the task defaults) every
run that resets the target first dumps the scope with `pg-dump-s3` (on the SSM runner when
`ssm.enabled`, section 16; otherwise in the Fargate `sql-runner`):
the target's tables that the selection rules of the table mapping select (the tables a reset
truncates) — `pg_dump -Fc --no-owner --no-privileges -t <table>…`, with their indexes,
constraints, defaults, triggers and owned sequences — streamed into
`s3://<bucket>/dumps/<project>/<taskKey>/<ts>-<runId>/target.dump` with a `manifest.json`
(SHA-256, size, project, task key, `tables`). SSE-KMS only (bucket policy), kept
`dump_retention_days` (14), readable only by the project's job role. No roles or passwords are
dumped. A failed dump stops the run before anything is truncated. A partitioned table in scope
is refused (`-t` would take it without its partitions): use `"method": "snapshot"` (11.1).

```bash
python scripts/orch.py dumps --project ledger --task core
python scripts/orch.py restore --project ledger --task core \
  --dump s3://<bucket>/dumps/ledger/core/<ts>-<runId>/ --confirm --wait
```

`restore` refuses without `--confirm` and while a run holds the task lock. It takes the task lock
itself (`LOCK#<taskArn>`, written only if none exists, `runId` `restore-<project>-<task>-<ts>`,
`executionArn` of its job-runner execution) before it starts anything and deletes it when it
ends, whatever the outcome; no run can start during a restore. If the CLI is killed, the
watchdog releases the lock once the execution ended and `lock_grace_seconds` passed. It always
waits (`--wait` is accepted and changes nothing). `pg-restore-s3` runs on the SSM runner when the
module has it (`ssm.enabled`, the registration says so), otherwise in the Fargate image;
`--runtime ssm|fargate` picks one, and a runtime the module lacks is refused before the lock.
The job-runner accepts from the operator only these restore jobs, with a dump under
`dumps/<project>/<task>/` of that very task, only while this restore holds the task lock for
that execution, and takes the connections and the baseline scope from the task registration and
the DMS task; a job-runner execution started by hand fails `JobNotInJobSet`.

The job (`pg-restore-s3`) then, in this order, and stops with nothing changed at the first
failure:

1. checks that the manifest's `project` and `taskKey` are those of the restore and the location
   is under `dumps/<project>/<taskKey>/`; a dump without `tables` in its manifest is refused;
2. downloads the dump and checks its SHA-256;
3. lists the archive (`pg_restore -l`) and keeps only the objects of the manifest's tables
   (never a schema, extension, function or another table): `--clean` drops and recreates
   exactly those, nothing else in their schemas; a table of the manifest missing from the
   archive stops it;
4. streams `pg_restore --clean --if-exists -L <list>` into `psql` (`ON_ERROR_STOP`) inside
   `BEGIN;` … and sends `COMMIT;` only after pg_restore exited 0, otherwise `ROLLBACK;`. Any
   failure leaves the target as it was before the restore.

A table of the manifest that an out-of-scope table references by a foreign key cannot be
dropped: the restore fails and rolls back; drop or defer that constraint with the database team.

After a successful restore the restored target becomes the new baseline (ADR 0006): the target
is fingerprinted (`checks/target_verify`, Data API) and `TARGET#BASELINE` records
`source=restore`, the `runId` of the run that took the dump, `restoreRunId` and `dumpUri`. The
last line of the output shows it:

```json
{"baseline": {"source": "restore", "dumpRunId": "<runId>", "restoreRunId": "restore-ledger-core-<ts>"}}
```

The next run is not stopped as `TargetChanged`, but it never resumes either: the restored rows
are from before the failed run while DMS still reports its tables as loaded, so a resume would
skip them. Pre-flight stops it as `MANUAL` with `RestoredTarget` ("target restored from dump
<runId>; a full reload is needed: re-run with --allow-reset"); start again with `--allow-reset`
for `reset` → `reload-target` of the whole scope (a dump is taken again first). A fresh DMS task
(no table statistics) follows the usual rules. `orch.py gate` refuses any run while the latest
baseline comes from a restore. Exit code 1 with `baseline not recorded`:
the restore itself succeeded, the fingerprint did not — the next run stops as `MANUAL`
(`TargetChanged`) as before. Without a Data API on the target no baseline is recorded (the
output says so).

### 11.1 Dump by cluster snapshot, restore from a snapshot (ADR 0007)

For a big target (the report's `advice` says so above `dump.snapshotAboveBytes`, default
100 GiB) ask `"dump": {"enabled": true, "method": "snapshot"}`. The task needs
`snapshot_dumps = true` in its defaults (module variable `projects`), otherwise the request is
refused before anything runs. The job `snapshot-target` creates a cluster snapshot of the target
cluster named `<project>-<taskKey>-<YYYYMMDD-HHMMSS>`, tagged `dms-orch:project`,
`dms-orch:task`, `dms-orch:run`, and waits until it is `available` (up to ~30 min in three
attempts); a failed or unfinished snapshot stops the run before anything is truncated.
`dump.json` next to the job result names it (the job's `manifestUri`):

```bash
aws s3 cp s3://<bucket>/runs/<project>/<runId>/jobs/dump/00-snapshot-target/dump.json -
aws rds describe-db-cluster-snapshots --db-cluster-snapshot-identifier <snapshotIdentifier>
```

The post job `snapshot-retention` deletes this task's tagged snapshots older than
`dump_retention_days`; snapshots taken by people (without the tags) are left alone.

**Restore from a snapshot is the database team's job, not the orchestrator's.** A cluster
snapshot restores only into a **new** cluster (`restore-db-cluster-from-snapshot`, then its
instances); pointing applications, DMS endpoints and secrets at it is a cutover. There is no
`orch.py restore` for snapshots. Hand the team the `snapshotIdentifier` and the run id; after
the target is back (same endpoint or a re-registered one), run
`orch.py start … --operation plan` and expect
`MANUAL`: the baseline belongs to the failed run, so start again with `--allow-reset`.

## 12. Repeat post

A post job failed (bug in the job set, a transient database problem): DMS does not need to run
again.

```bash
python scripts/orch.py post <runId> --from-job <job> [--jobs-version <fixed version>] --wait
```

The repeat asks DMS for the outcome again (refused while the task still runs), takes the task
lock (refused while another run holds it or the run's first post still runs), skips the post
jobs before `--from-job` that already ended `ok` and runs the rest. `--jobs-version` takes the
post and on-failure jobs of a corrected job set version. It ends like any post: baseline, report,
notification, unlock.

Auto-resume after a transient DMS failure needs nothing from the operator: post hands the lock to
a new `migrate <runId>-r<n>` (`attempt` + 1, at most `autoResume.maxAttempts`), which resumes the
task from its checkpoint.

## 13. IAM authentication for Fargate jobs (ADR 0009)

A connection can log Fargate (`psql`) jobs in with a short-lived IAM database token instead of a
secret: `auth = "iam"`, `iam_user`, `host` and
`iam_policy_arn = module.<aurora>.iam_database_auth.users["<user>"].policy_arn` (an upstream
aurora module with `iam_database_users`, `create_iam_database_auth_policies` and
`create_iam_database_users`). The module attaches that policy to the project's job role; jobs
name the connection in `--connection` and no secret is read. The token lives 15 minutes, is
signed at each client start and reaches only psql's environment; TLS is required. DMS endpoints
and the Data API built-ins keep their secret connections.

A failed login looks like `password authentication failed`. Check, in this order:

1. the user exists in the engine (`engine_status` in the aurora module output);
2. it has the `rds_iam` role;
3. the job role has the policy: `aws iam list-attached-role-policies --role-name <name>-<env>-job-<project> --profile <profile>`;
4. the user has the grants the job needs (the token only logs in).

## 14. Where the target check runs (`preflight.verifyRuntime`, ADR 0008)

Pre-flight fingerprints the target before every start. The request field
`preflight.verifyRuntime` picks where:

| Value | Runs | Use |
|---|---|---|
| `auto` (default) | Fargate when the last baseline had more than `preflight.fargateAboveTables` (500) tables or a database larger than `preflight.fargateAboveBytes` (50 GiB), or the target has no Data API, provided the `sql-runner` image exists; the Lambda otherwise | nothing to set |
| `lambda` | `checks/target_verify` through the Data API (connection `target` with `data_api_cluster_arn`) | small targets |
| `fargate` | `verify-target` in the `sql-runner` image through psql; refused when the module has no image | big targets, no Data API |

Both give the same fingerprint, so a baseline from one runtime is compared with a check from the
other without a false `TargetChanged`. With neither the Data API nor the image no check runs:
the verdict comes from the DMS state alone and the report says so. `orch.py start` has no flag
for it; give it in the run request (Step Functions input of `migrate`), e.g.
`{"project": "ledger", "taskKey": "core", "operation": "plan", "preflight": {"verifyRuntime": "fargate"}}`.
In the report the pre-flight job `target-verify` shows the runtime that ran (`lambda` or `fargate`).

## 15. Aurora Serverless v2 with auto-pause

A cluster with min 0 ACU pauses after `seconds_until_auto_pause` of inactivity. A run against a
paused cluster starts slower: the first Data API call of pre-flight wakes it
(`DatabaseResumingException`, then possibly `DatabaseUnavailableException`, both retried for
about 3.5 minutes per call); psql and DMS connections wake it too. Nothing to do. A target check
that still fails with one of those errors means the cluster did not resume: check it in the RDS
console and start again. The e2e scenario `s1p` proves this path ([docs/testing.md](docs/testing.md)).

## 16. SSM runner instance (runtime `ssm`)

Long jobs — `pg-dump-s3`, `orch.py restore`, long DDL, bash or pwsh scripts — run with SSM Run
Command on a runner instance built from the team AMI, not in Lambda (15 minutes) and without a
container image. Turn it on with the module input `ssm`:

```hcl
ssm = {
  enabled           = true
  instance_ids      = ["i-0123456789abcdef0"]           # or target_tags = { Role = "dms-runner" }
  instance_role_arn = "arn:aws:iam::<account>:role/<runner-role>"
  runner_user       = "orch"                            # default; never root
}
```

The runner instance needs:

* the AMI's tools: `psql`, `pg_dump` and `pg_restore` (major version ≥ the target's), `bash`,
  `python3` (3.9 or later) with `boto3`, `runuser`, `sha256sum`; `pwsh` only for pwsh jobs.
  Nothing is downloaded from the internet at run time;
* the SSM agent online, with the `ssm`, `ec2messages` and `ssmmessages` endpoints
  (`network_contract`), STS, Secrets Manager, logs, KMS and the S3 gateway endpoint, and a
  security group that reaches the databases (the module's jobs security group fits);
* an instance profile role (`ssm.instance_role_arn`) with SSM management
  (`AmazonSSMManagedInstanceCore` or the platform's equivalent) plus the module output
  `ssm_runner_policy_json`: read `runtime/*` of the artifacts bucket, assume the project job
  roles, write command output to the jobs log group. Every project job role trusts this role.

How a job runs: the job-runner picks an online runner among the configured ones (a retry takes
the next one), sends the document `<name>-<environment>-orch-job` (or
`ssm.document_name_override`, registered from the output `ssm_document_content`) with the spec
URI and the project's job role, and polls the invocation every 30 s. The document, as root,
downloads `runtime/<sha256>/sql-runner.zip` (the module uploads it on apply), checks its SHA-256,
unpacks it into a fresh `/var/tmp/orch-job.*` directory and starts `orch_job.py --assume-role
<job role> --command-from-spec` as `runner_user` (created as a system user if missing). orch-job
assumes the job role through a profile that renews it for jobs longer than an hour, reads the
command from `spec.json`, writes the connections file (0600, `runner_user`) and removes it and
the whole directory at the end, whatever the outcome. The result comes from S3 as for Fargate.

Where to look when a job fails:

| Message in the report | Meaning |
|---|---|
| `no SSM runner instance online` | no configured instance has `PingStatus = Online`: check the agent, the endpoints, `instance_ids` / `target_tags` |
| `SSM sendCommand failed: …` | the document or the instance is refused (wrong name, instance not managed, IAM) |
| `no result.json from SSM command <id> on <instance> (status …, exit code 96)` | a tool is missing on the AMI (`psql`, `python3` with `boto3`, …); the command output says which |
| `… exit code 97` | the runtime zip does not match its SHA-256: apply the module again |
| `… exit code 2` | orch-job could not use the job role (trust of `ssm.instance_role_arn`, STS endpoint) |
| `SSM command <id> Failed, exit code N` | the job command failed: command output in the jobs log group, stream `<command id>/<instance id>/orchJob/stdout` |
| `… (did not finish in time)` | no terminal status within the job timeout + 15 minutes: check the instance |

Anyone who can open an SSM session on the runner or run commands on it sees the job files and the
connections file while a job runs, and can use the instance profile (see
[docs/security.md](docs/security.md)): keep session access to the runners as narrow as access to
the job roles. The sandbox has a stand-in runner (`TF_VAR_ssm_runner=true`, Amazon Linux 2023
with the tools installed at boot, or `runner_ami_id` for the team AMI); e2e `s1ssm` runs the
reset and post jobs on it, `s10` the dump and the restore.
