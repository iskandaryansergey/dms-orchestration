# Troubleshooting

Symptoms that AWS produces around a run, their cause and what to do. They were observed in the
sandbox; the evidence and the measurements are in the appendix
[Observed AWS behaviour](dms-observations.md). Run statuses and failure categories with their
next step are in [`RUNBOOK.md`](../RUNBOOK.md) §3–4; this page covers what those tables do not.

The last column names the code that handles the case (`file:name`), or the e2e scenario that
proves it ([testing.md](testing.md)).

## DMS

| Symptom | Cause | Action | Where in the code |
|---|---|---|---|
| Every table stays `Before load` with 0 rows, the task `running` for as long as you wait, the instance busy | the target endpoint has `MaxFileSize = 0`: the provider sends 0 when a dms module renders `postgres_settings` without `max_file_size` | set it to 32768 (`ModifyEndpoint`), or use a dms module that sends it (contract 2.6); the orchestrator refuses to start such a task | `src/lambdas/inspect_task/handler.py:endpoint_problems`, `examples/sandbox/scripts/sandbox.py:fix_target_max_file_size` |
| `StartReplicationTask` refused: "Test connection … should be successful for starting the replication task", often right after `terraform apply` | DMS starts a task only after every endpoint passed `TestConnection`, and `ModifyEndpoint` (every apply that touches an endpoint) resets that state and starts a new test | keep `builtin:checks/connections` first in `pre`: it tests untested or failed endpoints and waits for a running test | `src/lambdas/builtin_jobs/handler.py:check_connections`, `src/shared/dms_outcome.py:evaluate_outcome` |
| A load finishes with tables in `Table error`, the task is `stopped`, not `failed` | DMS does not fail a task for a table error (`SUSPEND_TABLE`); the cause is only in the task log | default `failOnTableErrors = true` fails the run as `DmsTableErrors` with the category from the log; fix it and resume | `src/shared/dms_outcome.py:evaluate_outcome`, `src/lambdas/classify_failure/handler.py:log_tail` |
| A resume leaves the errored table in `Table error` with 0 rows | `resume-processing` does not reload tables in `Table error` | nothing: a task with errored tables is resumed with `reload-target` and a full reset | `src/shared/dms_outcome.py:decide` |
| `REJECTED` `InvalidStartType` for `reload-target` or `resume-processing` | DMS accepts only `start-replication` for a task that never ran | leave `startType` on `auto` | `src/shared/dms_outcome.py:decide` |
| `post` is started by the watchdog a few minutes after the load, not by the DMS event | `DescribeReplicationTasks` lags the `REPLICATION_TASK_STOPPED` event, by more than 30 s on `reload-target` | nothing: the event handler re-describes for up to 120 s, the watchdog is the backstop | `src/lambdas/dms_event_handler/handler.py:RECHECKS`, `src/lambdas/watchdog/handler.py:run` |
| An external `StopReplicationTask` is refused ("is currently not running") during a short load | DMS accepts a stop only around the end of a small full load | to interrupt on purpose, hold the target (a lock on one table), then stop | `examples/sandbox/scripts/e2e.py:s2` |

## Step Functions

| Symptom | Cause | Action | Where in the code |
|---|---|---|---|
| `StepFunctions.ExecutionAlreadyExistsException` when `post` starts a resume | an execution of that name already exists with another input, or ended (name idempotency holds only while the first one runs) | nothing: it counts as "already started" and the post ends normally; a repeat of post uses `orch.py post` | `modules/dms-orchestration/asl/post.asl.json:StartResume`, `examples/sandbox/scripts/e2e.py:idem_sfn` |
| A Catch on `InvalidResourceStateFault` never matches | inside a state machine the DMS error is named `DatabaseMigration.InvalidResourceStateException`, not the API name | catch the Step Functions name | `modules/dms-orchestration/asl/migrate.asl.json:StartReplicationTask` |
| `post` reads `context.json` back as a quoted JSON string | the S3 `putObject` integration JSON-encodes a string `Body` once more | pass the object itself as `Body` | `modules/dms-orchestration/asl/migrate.asl.json:SaveContext` |
| AWS TestState fails on an undeclared variable or a missing task token that the local runner accepted | TestState is stricter than the local runner | declare every variable the state assigns in the case | `tests/sfn/make_cases.py`, `tests/sfn/run_teststate.py` |

## Locks and verdicts

| Symptom | Cause | Action | Where in the code |
|---|---|---|---|
| `FAILED` `RunAlreadyActive` while nothing runs, after a `post` died | the lock of a dead run waits for its cleanup | a dead `post` releases its run's lock within seconds; an orphaned lock goes after the watchdog grace (`lock_grace_seconds`, 300 s); then start again | `src/lambdas/release_lock/handler.py:handler`, `src/lambdas/watchdog/handler.py:run`, `examples/sandbox/scripts/e2e.py:postdie` |
| `MANUAL` `RestoredTarget` after `orch.py restore` | the restored rows are from before the failed run while DMS still reports its tables as loaded: a resume would skip them | start again with `--allow-reset` (`RUNBOOK.md` §11) | `src/shared/preflight.py:verdict`, `examples/sandbox/scripts/e2e.py:s10` |
| `MANUAL` `TargetChanged` with only `rowsChanged` after an Aurora failover | PostgreSQL resets the row counters after a crash or a stats reset | confirm nothing else changed, then `--allow-reset` | `src/shared/preflight.py:diff` |

## Databases

| Symptom | Cause | Action | Where in the code |
|---|---|---|---|
| `DatabaseResumingException`, then `DatabaseUnavailableException`; the first run of the day is slower | Aurora Serverless v2 with min 0 ACU auto-paused; the first call wakes it | nothing: Data API calls retry for about 3.5 minutes; if it still fails, check the cluster in the RDS console | `src/lambdas/builtin_jobs/handler.py:WAKING_ERRORS`, `examples/sandbox/scripts/e2e.py:s1p` |
| A restore fails on `SET transaction_timeout` or a `\restrict` line | a PostgreSQL 17 client writes settings that an Aurora 16 server refuses | nothing: the restore drops that header line | `src/jobs/sql-runner/orch_dump.py:_copy_without_newer_settings` |
| An IAM-token job fails with `password authentication failed` | the user does not exist in the engine, lacks `rds_iam`, or the job role lacks the `rds-db:connect` policy | `RUNBOOK.md` §13 | `src/jobs/sql-runner/orch_job.py:generate_db_auth_token` |

## Fargate

| Symptom | Cause | Action | Where in the code |
|---|---|---|---|
| A job container (uid 10001, read-only root) fails with `PermissionError` on `/tmp`, `/work` or `/out` | Fargate mounts task volumes empty and `root:root 0755`, without the image content | nothing with the module's task definition: an init container opens them first; own task definitions need the same | `modules/dms-orchestration/state_machines.tf` |
| `verify-target` on Fargate fails with `TypeError: string indices must be integers` in `fingerprint.py` | the psql wrapper aggregated `json_agg(q)` over a subquery aliased `q`, and the sequences query has a column `q`: PostgreSQL resolves a bare name to a column before a table alias, so the rows came back as strings | nothing with an image from this release: the wrapper aggregates the qualified whole row (`orch_verify_row.*`) | `src/jobs/sql-runner/orch_verify.py:psql_query`, `examples/sandbox/scripts/e2e.py:s8fargate` |
| A new image tag runs the old code | CodeBuild in the sandbox builds from the source zip that the last `terraform apply` uploaded, and ECR tags are immutable | after a code change: commit, `terraform apply`, then `sbx build-image <new sha>`; `build-image` refuses while the zip differs from the working tree | `examples/sandbox/scripts/sandbox.py:stale_build_sources` |

## Terraform

| Symptom | Cause | Action | Where in the code |
|---|---|---|---|
| The first full `apply` fails because a role created seconds earlier cannot be assumed | IAM eventual consistency | run the same `apply` again | `examples/sandbox/README.md` |
| `Invalid for_each argument` in the dms module when the endpoint secrets are created in the same plan | one dms module implementation keys a `for_each` on the secret ARN, unknown at plan | apply the secrets first (`-target`), or use a module with static keys (contract 2.3) | `docs/upstream-modules-contract.md` |
| A second `destroy` fails with `Invalid for_each argument` | the same `for_each` cannot be planned once the secrets are gone | destroy what is left by address (`terraform state list`) | `examples/sandbox/README.md` |
| `destroy` waits 20 minutes or more on subnets and security groups | Lambda functions in a VPC hold network interfaces (ENI) that AWS releases asynchronously | wait; an interrupted destroy is run again | `modules/dms-orchestration/lambda.tf` |
| `/aws/ecs/containerinsights/<cluster>/performance` is left after `destroy` | ECS creates that log group itself for Container Insights; it is not in the state | keep `container_insights = false` (default) or delete it; `verify_destroyed.py` lists it | `modules/dms-orchestration/variables.tf:container_insights`, `scripts/verify_destroyed.py` |

## AWS account limits

| Symptom | Cause | Action | Where in the code |
|---|---|---|---|
| `rds:CreateDBCluster` denied in a sandbox account | a guardrail policy that conditions on `rds:DatabaseClass`; Aurora requests carry no class | allow `db.serverless` and cluster creation in the guardrail | `examples/sandbox/main.tf` |
| The DMS instance class is not orderable | the smallest class offered in us-east-1 is `dms.t3.small` | use it (sandbox default) | `examples/sandbox/variables.tf:dms_instance_class` |
| The `dms` interface endpoint cannot be created in a subnet | the endpoint exists only in some availability zones (us-east-1b/c/d) | pick subnets in those zones | `examples/sandbox/variables.tf:availability_zones` |
