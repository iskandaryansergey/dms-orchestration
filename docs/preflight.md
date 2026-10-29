# Pre-flight

Before `migrate` starts DMS it checks the target and gives a verdict. Why and the full table:
[ADR 0006](adr/0006-preflight-verdicts.md). Code: `src/shared/preflight.py` (pure),
built-in job `checks/target_verify`, `inspect_task` in `preflight` mode.

## What runs

```
InspectTask ─► TargetVerify (job-runner, builtin checks/target_verify) ─► Preflight (verdict) ─► CheckVerdict
                     ▲                                                                       │
                     └──────────── PreJobs ◄── prepare (tables missing, pre-jobs make progress)
```

* Runtime (`preflight.verifyRuntime`, ADR 0008): `lambda` runs `checks/target_verify` through
  the Data API (connection `target` with `data_api_cluster_arn`); `fargate` runs `verify-target`
  in the sql-runner image through psql, with the same SQL and the same fingerprint JSON and
  summary (`src/shared/fingerprint.py`); `auto` (default) takes Fargate when the last baseline
  had more than `fargateAboveTables` (500) tables or a database above `fargateAboveBytes`
  (50 GiB), or the target has no Data API, provided the image exists, else the Lambda. With
  neither the Data API nor the image no check runs: the verdict comes from the DMS state alone
  and the report says so.
* The scope is the task's table mapping (selection rules, include/exclude, `%`). Objects in the
  scanned schemas that the rules do not select are listed as warnings and left alone.
* The expectation comes from the source catalogue (connection `source` with the Data API),
  filtered by the same rules.

## Verdicts and what the operator does

| Verdict | Run status | Meaning | Next step |
|---|---|---|---|
| `ready` | runs | first run, target empty in scope | — |
| `resume` | runs | interrupted (or a CDC resume) and the target equals the baseline of the last run | — |
| `prepare` | runs | tables missing; pre-jobs run, then the target is checked again | — |
| `reset` | runs | the target was dirty and `allowReset` was given: truncate every table in scope, then `reload-target` (`start-replication` for a new task) | check the report: `decision.resetTables` |
| `manual` | `MANUAL` (failed, severity high) | `TargetNotEmpty`, `TargetChanged`, `AlreadyCompleted`, `PreflightTablesMissing` or `ResetNotConfigured`; the report has the diff | find out who changed the target; then either undo it or run again with `--allow-reset` |
| `manual` | `MANUAL` | `RestoredTarget`: the target was restored from a dump (`orch.py restore`) and DMS still reports loaded tables, so a resume would skip them | run again with `--allow-reset` (full `reload-target`, `RUNBOOK.md` §11) |
| `reject` | `REJECTED` | the task is running, busy or needs attention | as before |

`plan` runs the check and reports the verdict without running anything.

## Baseline

At every end of `post` (success, failure, resume; not a dry run) the target is checked again and
its fingerprint becomes the baseline: `TARGET#<project>/<taskKey>` in DynamoDB (hash, S3 URI)
and the JSON in S3. `TABLE#<project>/<taskKey>/<schema>.<table>` keeps our copy of the DMS table
statistics. Both are keyed by the registration, so they survive a DMS task that Terraform
destroyed and created again (new ARN).

The fingerprint hashes tables (columns, types, PK, unique indexes, row count — exact up to
`preflight.exactCountMaxRows`, else the planner estimate — and the row counters
`n_tup_ins/upd/del`), foreign keys and sequences. Known limit: PostgreSQL resets the row
counters after a crash or a stats reset (an Aurora failover can do it): the next pre-flight then
sees "rows changed" and stops as `MANUAL`; confirm and run with `--allow-reset`.

## Promotion gate

```bash
python scripts/orch.py gate <runId>     # exit 0: SUCCEEDED and its baseline is still the latest
```

Pipelines call it before promoting to staging or prod. A `MANUAL` run, a failed run or a run
whose baseline was overwritten by a later run exits 2 with the reasons.

## Runtimes of the check

The psql variant (`verify-target` in the sql-runner image) and the Data API variant share
`src/shared/fingerprint.py`; `preflight.verifyRuntime` picks one (see "What runs" and ADR 0008).
Table mapping and task settings are checked by `inspect_task` before the start.
