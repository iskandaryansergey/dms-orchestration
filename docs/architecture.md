# Architecture

## Goal

Take DMS task execution out of the Terraform pipeline: checks, start, post-work or a failure
branch, a report, and one run per task at a time. One module instance serves many projects of
a business line. DMS, Lambda and the state machines come and go with the DMS workspace; the
migration journal (locks, runs, baselines, dumps) stays in a long-lived one.

## Picture

```
 workspace: upstream aurora/dms modules (DMS task start=false) · module dms_orchestration
 operator ─StartExecution─► [migrate] Resolve ─ Lock ─ Inspect ─ Preflight (cycle) ─ [Assess] ─ Pre ─ [Dump] ─ [Reset]
                                      ─ Arm (MIGRATION#) ─ StartReplicationTask ─ Report STARTED ─ end
                                         │ jobs                                   │
                                         ▼                                        ▼
                            [job-runner] Lambda | SSM runner | [Fargate]       AWS DMS task
                                                                                  │ DMS Replication Task State Change
                                                                                  ▼
                            dms_event_handler ─Describe─ claim MIGRATION# ─ StartExecution(<runId>-post) ─┐
                            watchdog (5 min) ─ lost event · dead execution · orphan lock · overdue ───────┤
                                                                                                          ▼
                                       [post] Load ─ outcome ─ Post jobs | Classify ─ OnFailure ─ Baseline (TARGET#)
                                              ─ Report ─ platform SNS topic ─ ReleaseLock   (auto-resume: new migrate)
 state: DynamoDB LOCK# RUN# MIGRATION# TARGET# TABLE#  ·  S3 jobs/ runs/ dumps/  ·  SSM registration
```

## Components

| Component | Responsibility |
|---|---|
| [`migrate`](../modules/dms-orchestration/asl/migrate.asl.json) | lock → inspect → pre-flight verdict → (assessment) → pre-jobs → (dump) → (reset) → arm → start DMS → report `STARTED`; never waits for DMS |
| [`post`](../modules/dms-orchestration/asl/post.asl.json) | started by the DMS event: outcome → post-jobs or failure branch → target baseline → report → SNS → unlock; can be repeated (`operation=post`, `fromJob`) |
| [`job-runner`](../modules/dms-orchestration/asl/job-runner.asl.json) | one job: `when`, Lambda, SSM or Fargate, retries, `onError`, `JOB#` record; sub-machine of both |
| [`dms_event_handler`](../src/lambdas/dms_event_handler/handler.py), [`handoff`](../src/shared/handoff.py) | event → describe → `evaluate_outcome` → claim `MIGRATION#` → start `post` |
| [`watchdog`](../src/lambdas/watchdog/handler.py) | lost events, finished task without `post`, dead executions, orphan locks, overdue migrations |
| [`release_lock`](../src/lambdas/release_lock/handler.py) | failed or aborted executions: unlock unless DMS already runs (then `post` unlocks) |
| [`preflight`](../src/shared/preflight.py), [`fingerprint`](../src/shared/fingerprint.py) | target fingerprint, diff against the expectation and the baseline, verdict (ADR 0006) |
| [`dms_outcome`](../src/shared/dms_outcome.py) | start decision (start type, tables to reset) and the outcome of a finished task |
| [`resolve_context`](../src/lambdas/resolve_context/handler.py), [`inspect_task`](../src/lambdas/inspect_task/handler.py), [`classify_failure`](../src/lambdas/classify_failure/handler.py), [`write_report`](../src/lambdas/write_report/handler.py), [`prepare_job`](../src/lambdas/prepare_job/handler.py) | request and registration; task state; failure category; report and notification attributes; job routing |
| [`builtin_jobs`](../src/lambdas/builtin_jobs/handler.py) | built-in checks: connections, grants, row counts, target check, snapshots, diagnostics |
| [`sql-runner`](../src/jobs/sql-runner/) (SSM runtime zip; optional Fargate image) | orch-job, psql, `pg_dump`/`pg_restore` to S3, `verify-target` (the target check in psql), IAM token login, python, bash, pwsh |
| [`orch.py`](../scripts/orch.py) | operator CLI: start, status, report, stop, repeat post, dumps, restore, gate |

## Job runtimes

A job runs in one of three runtimes ([ADR 0011](adr/0011-job-runtimes.md)). **Lambda** is the
default: built-in checks and `sql:` jobs (pg8000 vendored in the zip, `verify-full` TLS, read-only
dry runs), up to 840 s. **SSM** runs long jobs (`pg-dump`, `orch.py restore`, long DDL, bash or
pwsh) through Run Command on instances from the team AMI; the job-runner polls the command every
30 s. **Fargate** is optional: only a non-empty `fargate.images` creates the ECS cluster, task
definitions and ECS roles and adds the ECR endpoints to `network_contract`. The module never
builds an image; the hardened Dockerfile is for whoever chooses Fargate.

## Data

| pk / sk | Written by | Content |
|---|---|---|
| `LOCK#<taskArn>` / `LOCK` | `migrate`, removed by `post` or `release_lock` | runId, executionArn; `gsi2pk=LOCK#ACTIVE` |
| `RUN#<runId>` / `META`, `EVENT#…`, `JOB#…` | all | status, phase, both execution ARNs, report URI; timeline; job results |
| `MIGRATION#<taskArn>` / `ACTIVE` | `migrate` (arm), handler or watchdog (claim), `post` (delete) | `armed → claimed → posted`; `gsi2pk=MIGRATION#ACTIVE` until posted |
| `TARGET#<project>/<taskKey>` / `BASELINE` | `post`, `orch.py restore` | fingerprint hash and S3 URI after the last run or restore |
| `TABLE#<project>/<taskKey>` / `<schema>.<table>` | `post` | copy of `DescribeTableStatistics`; survives a recreated DMS task (new ARN) |

S3: `jobs/<project>/<version>/`, `runs/<project>/<runId>/` (`context.json` pinned by version on `RUN#`,
report; `jobs/<phase>/<nn>-<name>/` spec, result, fingerprint, `dump.json`),
`dumps/<project>/<taskKey>/<ts>-<runId>/` (SSE-KMS, only the project role, `dump_retention_days`).
With `dump.method = snapshot` a tagged cluster snapshot replaces the S3 dump (ADR 0007). Outside
the sandbox DynamoDB and S3 come from a long-lived workspace (`table_name`,
`artifacts_bucket_name`).

## Failures

What happens when a run, an execution or an event fails, and what the operator does:
[`RUNBOOK.md`](../RUNBOOK.md) §3–6; symptoms from AWS: [troubleshooting.md](troubleshooting.md).

## Decisions

* [ADR 0001](adr/0001-standard-workflow.md) — Standard workflows.
* [ADR 0002](adr/0002-callback-vs-polling.md) — EventBridge plus watchdog; `DescribeReplicationTasks` is the truth.
* [ADR 0003](adr/0003-jsonata.md) — JSONata; decisions live in tested Python.
* [ADR 0004](adr/0004-lock-in-dynamodb.md) — one run per task via a conditional DynamoDB item.
* [ADR 0005](adr/0005-two-machines.md) — two machines, the DMS event starts `post`.
* [ADR 0006](adr/0006-preflight-verdicts.md) — pre-flight verdicts and the target baseline.
* [ADR 0007](adr/0007-dump-by-cluster-snapshot.md) — dump by `pg_dump` to S3 or by cluster snapshot.
* [ADR 0008](adr/0008-target-check-runtime.md) — the target check in a Lambda or in psql on Fargate, one fingerprint.
* [ADR 0009](adr/0009-iam-database-auth-for-fargate-jobs.md) — IAM token login for Fargate jobs; DMS and the Data API keep secrets.
* [ADR 0010](adr/0010-upstream-modules-dependency.md) — the upstream modules as a configurable dependency of the sandbox.
* [ADR 0011](adr/0011-job-runtimes.md) — job runtimes: SQL in Lambda by default, long jobs on SSM, Fargate optional; the module never builds an image.

Security and the trust model: [security.md](security.md). Not in scope: CDC until steady state
and cutover (database team), sets of tasks in one run, approval pauses, serverless DMS, MySQL
and Oracle checks.
