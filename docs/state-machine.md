# State machines

Definitions (JSONata, rendered with Terraform `templatefile`):
[`migrate.asl.json`](../modules/dms-orchestration/asl/migrate.asl.json) (runs start here),
[`post.asl.json`](../modules/dms-orchestration/asl/post.asl.json) (started by the DMS event),
[`job-runner.asl.json`](../modules/dms-orchestration/asl/job-runner.asl.json) (one job, sub-machine
of both). Why two machines: [ADR 0005](adr/0005-two-machines.md).

A run is `migrate <runId>` → DMS → `post <runId>-post`; an auto-resume continues as
`migrate <runId>-r<n>`. No execution waits for DMS.

## migrate

```
ResolveContext ──fail──► FailInvalidRequest                          (nothing locked)
  │
AcquireLock ──ConditionalCheckFailed──► MarkRunEndedWithoutLock ─► FailWithoutLock (RunAlreadyActive)
  │            (a lock handed over to this run id by post is taken over)
InspectTask ──error──► ClassifyFailure
  │
CheckDecision ── reject ─────────────► WriteReport (REJECTED)
  │            ── wait (attempt > 1) ► PrepareStart, waitOnly   (DMS restarted the task itself: arm, no start)
  ▼
TargetVerify (Map → job-runner, checks/target_verify) ─► Preflight (verdict, ADR 0006)
  │
CheckVerdict ── reject / plan / manual ──► WriteReport (REJECTED / PLANNED / MANUAL)
  │          ── prepare ──► PreJobs ─► TargetVerify again (≤ preflight.maxIterations)
  │          ── attempt > 1 or pre-jobs done ──► CheckAssessment   (chained resume: pre-jobs already ran)
  ▼
PreJobs (Map → job-runner) ──error──► ClassifyFailure
  │
CheckAssessment ── enabled, not dry run ──► StartAssessment ⟲ DescribeAssessment
  │                                         ── failed / warning (failOnWarning) / timeout ──► ClassifyFailure
  ▼
ReadMigration (getItem MIGRATION#, consistent) ─► ArmGuard ── armed / claimed ──► ClassifyFailure (MigrationAlreadyArmed)
  │      ── nothing above truncates the target; a refusal above leaves it as it was ──
CheckReset ── decision.reset ──► DumpJobs (Map; when dump is on) ─► ResetJobs (Map) ──error──► ClassifyFailure
  │
PrepareStart (startedAt = now) ─► SaveContext (runs/<project>/<runId>/context.json for post)
  │
CheckContextVersion ── no VersionId ──► ClassifyFailure (ContextNotPinned: the bucket must be versioned)
  │
RecordContextVersion (RUN#.contextVersionId: post loads only this version)
  │
CheckDryRun ── dryRun ──► StartPostDryRun (post runs its jobs in dry mode) ─► Started
  │
Arm (MIGRATION#<taskArn> armed; condition: none or posted) ──exists──► ClassifyFailure (MigrationAlreadyArmed)
  │
CheckWaitOnly ── waitOnly ──► ReportStarted
  │
StartReplicationTask ── InvalidResourceStateException ──► RecordStartError ─► ReportStarted
  │                                                       (the handoff judges it: DmsStartRejected if DMS never moved)
  │                  ── other error (after retries) ─► Disarm ─► ClassifyFailure
  ▼
ReportStarted (RUN# STARTED, phase dms) ─► [NotifyStarted if notify.onStart] ─► Started (lock stays)

ClassifyFailure ─► OnFailureJobs (Map, failures ignored) ─► WriteReport ─► CheckNotify ─► Notify
                ─► ReleaseLock ─► Done ─► Planned | Failed
```

`TargetVerify` runs the pre-flight target check as one job: `checks/target_verify` in a
Lambda through the Data API, or `verify-target` in the Fargate `sql-runner` image through
psql, chosen by `resolve_context` from `preflight.verifyRuntime` (`auto`, `lambda`, `fargate`;
ADR 0008, [preflight.md](preflight.md)). Both give the same fingerprint; with neither runtime
the job list is empty and the verdict comes from the DMS state alone.

## Handoff (Lambdas, not a state machine)

`dms_event_handler` (any DMS event for a task with an armed `MIGRATION#`) and `watchdog`
(every 5 min) call `shared.handoff.check_migration`: describe the task → `evaluate_outcome` →
pending: nothing; terminal: claim `MIGRATION#` (`armed → claimed`, one winner) →
`StartExecution(post, name=<runId>-post, input={runId, taskArn, outcome, startedBy})` →
`posted`. A claim without `postExecutionArn` older than 2 min is repeated by the watchdog under
the same name; `ExecutionAlreadyExists` counts as started.

## post

```
LoadRun (resolve_context, mode post: saved context + outcome) ──fail──► FailLoad (release_lock frees the lock)
  │
TakeOverLock (LOCK# executionArn = this execution) ─► DeleteMigration     (best effort, never fail the run)
  │
CheckOutcome ── failure ──► ClassifyFailure
  ▼
PostJobs (Map) ──error──► ClassifyFailure
  │  finalStatus = SUCCEEDED
  ▼
WriteReport ─► CheckNotify ─► Notify ─► ReleaseLock ─► Done ─► Succeeded | Failed

ClassifyFailure ─► CheckAutoResume ── autoResume ──► HandOverLock (LOCK# runId = <runId>-r<n+1>)
                                                  ─► StartResume (migrate, no .sync) ─► WriteReport (RESUMED)
                                                     └─ start failed ─► GiveLockBack ─► OnFailureJobs
                                   └─► OnFailureJobs (Map, failures ignored) ─► WriteReport (FAILED)
```

## Invariants (checked by `tests/python/test_asl.py`)

* no state waits for DMS: no `.waitForTaskToken`, no heartbeat, no DMS polling, no `Wait`
  outside the job-runner retry backoff;
* migrate: every terminal after `AcquireLock` passes `ReleaseLock`, except `Started` (DMS or the
  dry-run post holds the lock); after `Arm` the lock is released only when the start is refused;
* `StartReplicationTask` is reachable only through `Arm` (condition `attribute_not_exists(pk) OR status = posted`);
* nothing destructive before every refusing check: `DumpJobs` and `ResetJobs` are reachable only
  through `CheckAssessment` (assessment passed or off) and the read-only Arm guard
  (`ReadMigration` → `ArmGuard`); `Arm` keeps its condition as the atomic write;
* post: every terminal after `LoadRun` passes `ReleaseLock`; `StartResume` only after
  `HandOverLock`, without `.sync`;
* the lock-contention path never reaches `ReleaseLock`;
* every `lambda:invoke` has the AWS-recommended Lambda retries and a Catch;
* DynamoDB expressions alias reserved words; SDK attribute type names (`Bool`, not `BOOL`).

## Variables

| Machine | Variable | Set in | Meaning |
|---|---|---|---|
| both | `runId`, `req`, `ctx` | ResolveContext / LoadRun | run id (= migrate execution name), request with defaults, registration + jobs |
| both | `inspect`, `jobCtx` | InspectTask / LoadRun | task state and completion spec, context passed to jobs |
| both | `attempt` | ResolveContext / LoadRun | 1 + number of chained resumes (`req.attempt`) |
| both | `startedAt` | PrepareStart / LoadRun | time before the start call (the failed run's for `waitOnly`) |
| migrate | `decision`, `waitOnly` | InspectTask, CheckDecision | start type / reset; arm without starting |
| migrate | `migration` | ReadMigration | `{status, runId}` of an existing `MIGRATION#` item (Arm guard) or null |
| post | `dmsOutcome`, `failure` | LoadRun | DMS outcome from the handoff: success payload or `{Error, Cause}` |
| post | `nextRunId` | CheckAutoResume | run id of the chained migrate |
| both | `classification`, `finalStatus`, `report` | ClassifyFailure, several, WriteReport | category; STARTED, PLANNED, REJECTED, SUCCEEDED, RESUMED or FAILED; report |

## Retry and timeouts

| State | Retry | On error |
|---|---|---|
| Lambda tasks | Lambda service errors, 6×, backoff 2, full jitter | ClassifyFailure (report states: still notify and unlock) |
| AcquireLock / Arm / HandOverLock / ReleaseLock | DynamoDB throttling, 5× | contention → reject / classify; ReleaseLock errors → Done (safety nets clean up) |
| StartReplicationTask | `InvalidResourceStateException` 0×; other task errors 4×, ≤ 60 s | InvalidResourceState → RecordStartError; else Disarm and classify |
| StartPostDryRun / StartResume | execution limit, timeout 3× | `ExecutionAlreadyExists` = already started |
| Notify | 3× | ignored |
| execution | — | `TimeoutSeconds` = 7 days for jobs; the DMS run itself is limited by `runTimeoutSeconds` → watchdog `MigrationOverdue` |

## Start type and reset (from `inspect_task`)

| Task state | `startType: auto` | Reset |
|---|---|---|
| `fresh` (ready, never ran) | `start-replication` | — |
| `interrupted`, policy `if-interrupted` | `resume-processing` | partial tables |
| `interrupted`, policy `always` | `reload-target` | all tables |
| `interrupted`, policy `never` | `resume-processing` | — |
| `completed`, `migrate` | reject `AlreadyCompleted` | — |
| `completed`, `resume` (CDC types) | `resume-processing` | — |
| `running` since the failed run started it (`attempt > 1`) | `wait` (arm, no start) | — |
| `running` / `busy` / `failed-move` | reject `TaskAlreadyRunning` / `TaskBusy` / `TaskNeedsAttention` | — |

Explicit start types are validated (`start-replication` only for a fresh task;
`reload-target` always truncates all tables; `never` + `reload-target` is rejected).
Pre-flight verdicts on top of this table (dirty target → `manual`, `allowReset`): ADR 0006.

## DMS outcome (`shared.dms_outcome.evaluate_outcome`, used by the handoff)

| DMS | Result |
|---|---|
| stopped, `FULL_LOAD_ONLY_FINISHED` | `COMPLETED` (or `COMPLETED_WITH_TABLE_ERRORS`; fails `DmsTableErrors` with `failOnTableErrors`, the default) |
| stopped, `STOPPED_AFTER_FULL_LOAD` / `CACHED_EVENTS` / `DDL_APPLY` / `AT_SERVER_TIME` / `AT_COMMIT_TIME` | `STOPPED_AT_CHECKPOINT` |
| stopped, `NORMAL` (not requested) | `DmsTaskStoppedUnexpectedly` |
| stopped, `RECOVERABLE_ERROR` / `FATAL_ERROR` / low memory / low disk | `DmsTaskFailed` |
| stopped, `RECONFIGURATION_RESTART` / `RECYCLE_TASK` | `DmsTaskStoppedUnexpectedly` (category DMS_RESTART, auto-resumable) |
| failed / failed-move | `DmsTaskFailed` with `LastFailureMessage` |
| deleting / not found | `DmsTaskDeleted` |
| `InvalidResourceStateException` on start and the task never moved | `DmsStartRejected` |
| terminal status older than the start call | pending (stale state of the previous run) |
| still `ready` 15 min after the start call | `DmsTaskNotStarted` |
| anything else | pending |

The first iteration is a full load (`completion: auto` = `task-stopped` for full-load tasks);
CDC until steady state and cutover belong to the database team.

## job-runner

```
Init ─► PrepareJob ──error──► JobFailed
          │
CheckSkip ── when false ──► RecordJob (skipped)
          │
CheckRuntime ── lambda ──► InvokeLambdaJob ─────────────────────────────┐
             └─ fargate ─► RunFargateJob ─► FetchFargateResult ─► Normalize ─┤
                                                                          ▼
                                                  RecordJob (JOB# item) ─► CheckStatus
CheckStatus ── ok | warn | skipped ─────────────────► JobSucceeded
            ── fail, attempt < max, idempotent ─────► WaitBeforeRetry (backoff) ─► CheckRuntime
            ── fail, onError continue | warn ───────► JobContinued
            └─ otherwise ───────────────────────────► JobFailed (error JobFailed)
```

A Fargate job is `fail` when the container exit code is non-zero or ECS failed, whatever
`result.json` says; no `result.json` is also a failure.

## Testing

* `tests/python/test_asl.py` — graph invariants and path scenarios of both machines with mocked
  tasks, using `tests/python/asl_runner.py` (real ASL, real JSONata via `jsonata-python`).
* `tests/python/test_handoff.py` — handoff with moto: two events → one `StartExecution`, a stale
  claim repeated under the same name, Describe lag, `release_lock` in the DMS phase.
* `tests/sfn/cases/*.json` — AWS TestState cases (generated by `tests/sfn/make_cases.py`);
  checked locally by `tests/python/test_sfn_cases.py`, run against AWS with `tests/sfn/run_teststate.py`.
* `modules/dms-orchestration/tests/defaults.tftest.hcl` — three machines, each naming the other,
  the event rule on both, the handoff Lambdas pointed at post.

`StartExecution` with the same name and input returns the same execution while it runs, a
different input answers `ExecutionAlreadyExists` (`e2e.py idem`, `docs/dms-observations.md` §9.1).
Inside a state machine the error is `StepFunctions.ExecutionAlreadyExistsException`, the name the
Catch of `StartResume` and `StartPostDryRun` uses; `e2e.py idem-sfn` triggers it for real.
