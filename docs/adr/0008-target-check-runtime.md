# ADR 0008 — The target check runs in a Lambda or in psql on Fargate, with one fingerprint

- Status: accepted
- Date: 2026-10-29

## Context

Pre-flight and the baseline (ADR 0006) fingerprint the target through the Data API in the
built-in Lambda job `checks/target_verify`. A Lambda stops after 15 minutes and each Data API
call after 45 seconds; a target with thousands of tables, or exact counts on big tables, does
not fit. Some targets have no Data API at all. The sql-runner image already reaches the
database with psql over TLS.

## Decision

* The fingerprint SQL and its assembly live in `src/shared/fingerprint.py`; a transport is a
  function `query(sql, **params) -> rows`. The Lambda job binds parameters through the Data API;
  `verify-target` (sql-runner) runs each query as `SELECT json_agg(...)` through `psql -X -A -t`
  with the SQL on stdin and the parameters as psql variables (`:'name'`, quoted by psql).
* Both write `target-fingerprint.json` next to the job result and return the same summary
  (`fingerprintUri`, `fingerprintHash`, `tables`, `rows`, `expected`, `outOfScope`,
  `databaseBytes`). The job keeps the name `target-verify`, so pre-flight, the resume baseline
  and the end-of-post baseline read it unchanged.
* `preflight.verifyRuntime = auto | lambda | fargate`, default `auto`: Fargate when the last
  baseline had more than `fargateAboveTables` (500) tables or more than `fargateAboveBytes`
  (50 GiB), or the target has no Data API, and the sql-runner image exists; otherwise the
  Lambda. `fargate` without the image is refused before the run.
* An equivalence test runs one fixture catalogue through both transports and compares the
  fingerprints and their hashes.

## Consequences

* A big target costs a Fargate task (about a minute to start) instead of a Lambda call.
* The restore baseline (`recordBaseline`, `orch.py restore`) stays in the Lambda job.
* psql's JSON and the Data API's JSON may differ for types the fingerprint does not use today;
  new columns in the fingerprint need a case in the equivalence test.
