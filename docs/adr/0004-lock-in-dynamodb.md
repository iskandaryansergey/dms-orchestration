# ADR 0004 — One active run per task, lock in DynamoDB

- Status: accepted
- Date: 2026-10-27

## Context

Two concurrent runs on the same DMS task would race on `StartReplicationTask`,
reset the target twice or run post-jobs on half-loaded data. Unique execution
names do not prevent this (each run gets a new name).

## Decision

- A lock item `pk = LOCK#<taskArn>, sk = LOCK` is created by the state machine with
  `PutItem ... ConditionExpression attribute_not_exists(pk)` right after the request
  is validated. A conditional failure ends the run with `RunAlreadyActive` within
  seconds, before anything touches DMS.
- The lock stores `runId` and `executionArn`. `ReleaseLock` deletes it with
  `ConditionExpression runId = :runId` on **every** terminal path of the state
  machine (success, failure, plan, rejection).
- Safety nets for executions that die outside the state machine's own error
  handling (manual `StopExecution`, timeout):
  1. EventBridge rule on `Step Functions Execution Status Change`
     (`FAILED | TIMED_OUT | ABORTED`) → Lambda `release_lock` (checks `executionArn`).
  2. `watchdog` lists active locks (sparse GSI) and removes locks whose execution
     is no longer `RUNNING`.
- No TTL-based expiry: a silently expiring lock during a multi-day load would allow
  a second run.

## Consequences

- The table is single-table design (`LOCK`, `RUN`, `WAITER`, `EVENT`, `JOB` items),
  PITR and KMS encryption on, deletion protection configurable.
- An orphaned lock is always visible (item + log line + metric) and is removed by
  the safety nets, not by time.
- Operators can inspect locks with one `GetItem`; the RUNBOOK documents manual
  removal for the rare case both safety nets are down.
