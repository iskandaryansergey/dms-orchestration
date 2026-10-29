# ADR 0007 — Dump before a reset: pg_dump to S3 by default, cluster snapshot as an option

- Status: accepted
- Date: 2026-10-29

## Context

A reset truncates the target scope. The default dump (`pg-dump-s3`) streams
`pg_dump -Fc` of the scope into `dumps/` in S3 from Fargate and is restored in place by
`orch.py restore`. A sandbox run measured 3.2 MB in 2.9 s; a target of hundreds of GiB would
keep a Fargate task busy for hours. An Aurora cluster snapshot takes minutes regardless of size,
but it is restored only as a new cluster, and switching applications to another cluster is a
cutover: the database team's zone, not the orchestrator's.

## Decision

* `dump.method = pg-dump | snapshot`, default `pg-dump`. `snapshot` is allowed only for a task
  with `snapshot_dumps = true`; only then does the project's job role get snapshot rights.
* The built-in job `dump/cluster_snapshot` (Lambda, the project job role) calls
  `CreateDBClusterSnapshot` on the cluster of the `target` connection (`cluster_arn`, else
  `data_api_cluster_arn`) and polls `DescribeDBClusterSnapshots` until `available`. One Lambda
  waits 10 min; still `creating` is a failure that the job-runner retries (3 attempts, about
  30 min). A retry finds the run's snapshot by its `dms-orch:run` tag instead of creating
  another one.
* Name `<project>-<taskKey>-<YYYYMMDD-HHMMSS>`, tags `dms-orch:project`, `dms-orch:task`,
  `dms-orch:run`: no host, user or secret. `runs/<runId>/dump.json` records the snapshot.
* IAM: create (and describe) on the registered target clusters; creating a snapshot needs the
  request tag `dms-orch:project = <project>` on `cluster-snapshot:<project>-*`; describe and
  delete of snapshots need the resource tag `dms-orch:project = <project>`.
* Retention: the post job `snapshot-retention` (`cleanup/cluster_snapshots`) deletes this task's
  snapshots older than `dump_retention_days`. Snapshots without both the project and the task tag
  (taken by people) are never touched.
* Advice: `checks/target_verify` reports `pg_database_size`; when a `pg-dump` run's target is
  larger than `dump.snapshotAboveBytes` (default 100 GiB), the report advises `snapshot`.
* Restore from a snapshot is not automated: RUNBOOK §11.

## Consequences

* Big targets get a dump in minutes; restoring it is a planned operation of the database team.
* Snapshots are not in Terraform state and survive `destroy` of the workspace; the sandbox
  scenario `s10snap` deletes its own, and `verify_destroyed` lists manual cluster snapshots.
* The job role reads snapshot metadata of the target cluster (the cluster filter lists every
  manual snapshot of it); it can change only snapshots with its project tag.
