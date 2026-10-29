# ADR 0001 — Step Functions Standard workflows

- Status: accepted
- Date: 2026-10-27

## Context

A DMS full load runs for hours or days. The orchestrator must wait for the task
outcome, run pre/post jobs (some of them long, on Fargate), and must not lose or
duplicate a step. Step Functions offers two workflow types:

| | Standard | Express |
|---|---|---|
| Max duration | 1 year | 5 minutes |
| Execution semantics | exactly-once | at-least-once |
| `.sync` / `.waitForTaskToken` | supported | not supported |
| History | 25,000 events, 90 days, visible in console | CloudWatch Logs only |
| Price | per state transition | per request + duration |

## Decision

All orchestrator state machines (`task-runner`, `job-runner`) are **Standard**.
Jobs run as nested executions (`states:startExecution.sync:2`) so each execution
history stays small and every job has its own execution ARN for the report.

## Consequences

- Runs can wait for DMS for days; the run timeout is a module variable
  (`run_timeout_seconds`, default 7 days).
- Exactly-once state transitions: a retried `StartReplicationTask` is still guarded
  (see ADR 0002) but Step Functions itself never replays a completed state.
- Cost is per transition. A run is roughly 30–60 transitions plus jobs, which is
  negligible compared with the DMS instance.
- Execution history (input/output) is retained for 90 days; payloads carry only
  ARNs, never secrets.
