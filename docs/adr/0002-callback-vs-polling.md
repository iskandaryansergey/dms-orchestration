# ADR 0002 — Wait for DMS with a callback, keep a watchdog as safety net

- Status: accepted; the callback part is superseded by ADR 0005
- Date: 2026-10-27

## Context

After `StartReplicationTask` the orchestrator must wait until the task finishes.
The design review with the platform team asked to listen to DMS events (EventBridge)
rather than poll inside Lambda.

Facts that shape the design:

- `.waitForTaskToken` works only with APIs that accept a task token.
  `StartReplicationTask` has no such field, so the wait cannot be attached to the
  start call itself.
- DMS publishes `DMS Replication Task State Change` events to EventBridge, but the
  documentation lists exact `eventType` strings only for "started". Event delivery
  is at-least-once and events can be lost when a rule is disabled or misconfigured.
- `DescribeReplicationTasks` is the source of truth (`Status`, `StopReason`,
  `ReplicationTaskStats`, `LastFailureMessage`).

Options:

1. Polling loop in the state machine (`Wait` → `Describe` → `Choice`) — simple, but
   for a multi-day load this burns transitions and history events (25k limit), and
   events are the preferred mechanism.
2. Callback: a `lambda:invoke.waitForTaskToken` state stores the token; an
   EventBridge rule wakes a handler that re-reads DMS and completes the token.

## Decision

Option 2, hardened:

- `register_waiter` stores the token in DynamoDB (`WAITER#<taskArn>`) and **then**
  immediately describes the task; if the task already finished it completes the
  token itself (closes the "event before registration" race).
- `dms_event_handler` does not parse `eventType`. Any event for a registered task
  ARN means "describe the task again"; the decision is made by the pure function
  `shared.dms_outcome.evaluate_outcome`.
- Exactly one callback: every sender first does a conditional update
  `SET consumed = true IF consumed = false`; only the winner calls `SendTask*`.
- `watchdog` (EventBridge Scheduler, every 5 minutes) walks active waiters, sends
  heartbeats (`HeartbeatSeconds = 900` = 3 watchdog periods) and completes waits
  whose events were lost. It also removes waiters/locks of dead executions.

## Consequences

- No transitions are spent while DMS runs; the run's history stays short.
- A lost event delays completion by at most one watchdog period.
- If the watchdog itself stops, the heartbeat times out after 15 minutes and the
  run fails loudly (`WaitTimeout`) instead of hanging; an alarm on watchdog
  invocations is part of the module.
- The handler code is shared by three callers; the decision logic is a pure
  function with table-driven unit tests.
