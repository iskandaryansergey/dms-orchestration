# ADR 0006 — Pre-flight is a cycle with a verdict: ready | prepare | resume | reset | manual

- Status: accepted (design review with the platform team)
- Date: 2026-10-28

## Context

Pre-flight must be a repeatable cycle: "what is missing, what is extra", then continue,
reset or stop for a human; a spoiled target must never move on to staging or prod.
A "CDC slot" is a point to continue from after a
failure; the first iteration is a full load and cutover belongs to the database team.
DMS table statistics vanish when the DMS workspace is destroyed and
the DMS task is recreated, so they cannot be the only memory.

Facts from the live sandbox runs: `resume-processing` finishes unfinished tables but does not
reload tables in `Table error`; those need `reload-target` with a full reset.

## Decision

Inputs: task state (`fresh | interrupted | completed | running | busy`), DMS table
statistics, our copy `TABLE#<project>/<taskKey>/<schema>.<table>`, the target fingerprint now
versus `TARGET#<project>/<taskKey>/BASELINE` written by the last `post`, the expected table set (selection
rules × source catalogue), and the request flags `allowReset`, `targetReset.policy`.

| # | Situation | Verdict | Action |
|---|---|---|---|
| 1 | expected tables missing, the job set has `pre` jobs | `prepare` | pre-jobs, check again while they make progress; still progressing at `preflight.maxIterations` (3) → `manual`; no progress → warning, DMS creates the tables (`DO_NOTHING`) |
| 2 | task fresh, no baseline, scope tables empty | `ready` | `start-replication` |
| 3 | task fresh but rows in scope or `TABLE#` exist (DMS recreated) | `manual`; `reset` with `allowReset` | truncate scope → `start-replication` |
| 4 | interrupted, fingerprint = baseline | `resume` | truncate partial tables → `resume-processing`; `Table error` → `reload-target` |
| 5 | interrupted, fingerprint ≠ baseline | `manual`; `reset` with `allowReset` | reset-jobs → `reload-target` |
| 6 | completed, `operation=migrate` | `manual` (`AlreadyCompleted`); `reset` with `allowReset` | reset → `reload-target` |
| 7 | running / busy / needs attention | `reject` | as today: `REJECTED` (`TaskAlreadyRunning`, `TaskBusy`, `TaskNeedsAttention`) |

- The fingerprint (built-in job `checks/target_verify` through the Data API, or `verify-target`
  through psql on Fargate with the same SQL, ADR 0008) covers the schemas of the table mapping:
  tables, columns and types, PK and
  unique indexes, FKs, sequences, `reltuples`, `n_tup_ins/upd/del`, exact `count(*)` up to
  `preflight.exactCountMaxRows`, the `dms_control` table. Hash in DynamoDB, full JSON in
  `runs/<runId>/jobs/<preflight|baseline>/00-target-verify/target-fingerprint.json`. Objects
  outside the scope give a warning only
  (many-to-one: other sources live in the same target).
- `manual` = run status `MANUAL`, a report with the diff (missing / extra / changed /
  rowsChanged), SNS with `severity=high`, lock released.
- Promotion guard: `orch.py gate <runId>` exits non-zero unless the run `SUCCEEDED` and the
  baseline it recorded is still the latest (no run touched the target since; a fresh
  fingerprint is compared by the next pre-flight); pipelines call it before staging/prod.
- Decisions are pure functions in `src/shared/preflight.py` with a test per table row.
- `TARGET#` and `TABLE#` are keyed by the registration (`<project>/<taskKey>`), not by the
  task ARN as the plan first said: Terraform gives a recreated task a new ARN, and row 3
  exists precisely for that case.
- `post` records the baseline at every end (success, failure, resume; not for a dry run); on
  the auto-resume path it records it *before* starting the next `migrate`, whose pre-flight
  compares against it. The record says `source=post`, `runId` = that run.
- A successful `orch.py restore` records a new baseline too: the
  restore puts back a known dump, so the target state is known and intended. Without it the
  next run stopped as `MANUAL`/`TargetChanged` (`rowsChanged` on every table) and needed
  `allowReset`. After the restore job, `checks/target_verify` runs in the `restore` phase and
  writes `TARGET#BASELINE` with `source=restore`, `runId` = the run that took the dump (from
  its manifest), `restoreRunId`, `dumpUri`. No baseline when the target has no Data API (then
  the old behaviour stays: the next run may stop as `MANUAL`).
  - `orch.py gate` refuses a run whose latest baseline has `source=restore`: the target is
    the dump run's starting point, not its result.
  - Never `resume` after a restore: the dump is the target from before the run, but the DMS
    table statistics still say "loaded" — a resume would skip those tables and leave the
    migrated rows out, silently. When the baseline has `source=restore` and the task's table
    statistics show loaded or partial tables, the verdict is `manual` with error
    `RestoredTarget` ("target restored from dump <runId>; a full reload is needed: re-run
    with --allow-reset"); with `allowReset` it is `reset` → `reload-target` of the whole scope
    (a dump is taken again). A fresh task after a restore has no statistics and follows the
    usual rows (2, 3).

## Consequences

- A DBA edit or a recreated DMS task never leads to silent duplicates: the run stops as
  `MANUAL` unless the operator passed `allowReset`.
- Each run ends by writing the baseline, which closes the loop for the next run.
- State that must survive the DMS workspace (DynamoDB, S3) lives in a long-lived workspace on the
  target platform (`table_name`, `artifacts_bucket_name`); the sandbox keeps everything in one.
