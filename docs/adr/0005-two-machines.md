# ADR 0005 — Two machines: `migrate` ends after the DMS start, `post` starts on the DMS event

- Status: accepted (design review with the platform team); replaces the waiting part of ADR 0002
- Date: 2026-10-28

## Context

The MVP `task-runner` waits for DMS inside one execution (`lambda:invoke.waitForTaskToken`,
heartbeat 900 s, `register_waiter`, `WAITER#`). It works in five sandbox runs, but the design
review with the platform team asked for two machines: pre-flight plus the start, and a separate post phase triggered by the
completion event, with its own state and report — no execution hangs for an hour
in pending.

Options:

1. Keep one `task-runner` with the callback (proven, Standard waits up to a year at no cost).
2. Two machines: `migrate` (lock → inspect → pre-flight → pre-jobs → start DMS → end) and
   `post` (DMS outcome → post-jobs or failure branch → baseline → report → unlock).
3. Three entry points: `preflight`, `migrate`, `post` — the same pre-flight logic twice
   (the first machine is pre-flight and the actual execution).

## Decision

Option 2, with `job-runner` kept as an internal sub-machine of both (three definitions,
two entry points).

- Before `StartReplicationTask`, `migrate` writes `MIGRATION#<taskArn>/ACTIVE`
  (`status=armed`, condition `attribute_not_exists`). Writing before the start closes the
  "event before registration" race by order alone.
- `dms_event_handler` keeps its job (describe, `evaluate_outcome`, no event text parsing) and
  only changes the action: on a terminal outcome it claims the item
  (`armed → claimed`, one winner) and calls `StartExecution(post, name="<runId>-post")`,
  then marks it `posted` with the post execution ARN.
- `watchdog` walks `MIGRATION#ACTIVE`: lost event → same path; `claimed` older than 2 min
  without `postExecutionArn` → repeat `StartExecution` with the same name; overdue migration
  → `MigrationOverdue` alarm.
- The lock (`LOCK#<taskArn>`) is held from `migrate` until the end of `post`. `release_lock`
  reads the phase from `RUN#`: before the DMS start it removes `MIGRATION#` and the lock;
  after the start (`phase=dms`) it leaves both — `post` will come. A `post` that ends FAILED,
  TIMED_OUT or ABORTED in any phase releases the lock of its run (seen live: post failed in
  `LoadRun`, before `TakeOverLock`, and the lock waited for the watchdog's 300 s grace): only
  while the lock still has that `runId` and is held by this post or by the run's own migrate;
  a lock handed over to an auto-resume, or held by a repeat of post, stays. Its `posted`
  `MIGRATION#` item is removed with it.
- Auto-resume is a new `migrate` execution started by `post` (`attempt+1`, `parentRunId`,
  lock handed over by a conditional update), not a loop inside one execution.
- A direct EventBridge → Step Functions target was rejected: the handler must describe the
  task and claim before starting `post`.

## Consequences

- No execution waits for DMS; histories stay short; `post` can be repeated on its own
  (`operation=post`, `fromJob`).
- Two executions per run must be correlated: `RUN#` stores both ARNs, `orch.py status`
  shows both.
- `StartExecution` idempotency by name is the second guard behind the claim: Standard
  returns the same execution for the same name and input, `ExecutionAlreadyExists` for a
  different input — treated as "already started". The e2e scenario `idem-sfn` raises a real collision from
  `post`'s `StartResume` against AWS (`docs/testing.md`).
- Removed: `register_waiter`, `.waitForTaskToken`, `heartbeat_seconds`,
  `WAITER#*`, callback resend, `src/shared/waiting.py`, `task-runner.asl.json`.
