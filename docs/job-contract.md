# Job contract: pre, reset, post and on_failure jobs

A **job set** describes what runs around the DMS task. Project teams own it; the
orchestrator runs it. Start with the tutorial below; the rest of this page is the contract.
Examples: [`examples/jobs/minimal`](../examples/jobs/minimal) (the tutorial) and
[`examples/jobs/postgres-basic`](../examples/jobs/postgres-basic) (schema diff, DDL, scripts).

## Authoring a first job set

Ten minutes from an empty directory to a job set a run uses.

**1. The structure.** A bundle is a directory with `jobset.yaml` and the files its jobs name:

```
my-jobs/
├── jobset.yaml
└── sql/indexes.sql
```

**2. The job set.** Four phases; each job is either a built-in Lambda job (`handler:`) or a
command in the Fargate `sql-runner` image (`command:`). This is
[`examples/jobs/minimal/jobset.yaml`](../examples/jobs/minimal/jobset.yaml):

```yaml
# A first job set: check the DMS connections, empty partly loaded tables before a resume,
# build the indexes after the load and compare row counts.
#
# Connections (registered in Terraform, projects.<p>.tasks.<t>.connections): source and
# target, both with data_api_cluster_arn (the row counts run through the Data API).
#
# Validate: uv run python scripts/jobset_tool.py validate examples/jobs/minimal
# Upload:   scripts/sync-jobs.sh <project> <version> examples/jobs/minimal
schemaVersion: 1
description: connection check, reset of partial tables, indexes after the load, row counts

defaults:
  runtime: fargate            # jobs with a command run in the sql-runner image
  image: sql-runner           # key in the module variable fargate.images
  timeoutSeconds: 1800

phases:
  pre:
    - name: check-connections
      handler: builtin:checks/connections   # a built-in Lambda job: TestConnection where needed

  reset:
    - name: truncate-tables
      command: [psql-run, --connection, target, --truncate, "{{ ctx.resetTables }}"]

  post:
    - name: indexes
      command: [psql-run, --connection, target, --file, sql/indexes.sql]

    - name: row-counts
      handler: builtin:checks/row_counts
      with: { source: source, target: target }

  on_failure:
    - name: collect-diagnostics
      handler: builtin:diagnostics/dms_dump
      onError: continue
```

* `pre` runs before DMS starts. `check-connections` comes first in every job set: DMS refuses
  to start until its endpoints passed a connection test.
* `reset` runs only when the run decides partly loaded tables must be emptied
  ([When does `reset` run?](#when-does-reset-run)); `{{ ctx.resetTables }}` is the list it
  decided on.
* `post` runs after a successful load; `on_failure` after a failure that is not resumed.
* **Runtime.** A job with `handler:` is a Lambda (up to 840 s, Data API for SQL); a job with
  `command:` runs on Fargate in the image named by `image` (psql, mysql, python, bash, pwsh).
* **Conditions.** Add `when: "<expression>"` to run a job only sometimes, for example
  `when: "ctx.startType == 'reload-target'"` ([Conditions and templates](#conditions-and-templates)).
* **SQL.** `psql-run --file` runs files from the bundle; `--template` fills placeholders safely
  ([SQL templates](#sql-templates---template)). Write SQL that can run twice
  (`IF NOT EXISTS`): a job may be retried.

**3. Validate and upload.** The tool checks the schema, the job names and that every file a
command names exists; versions in S3 are immutable (use a git sha or tag):

```bash
uv run python scripts/jobset_tool.py validate examples/jobs/minimal
scripts/sync-jobs.sh ledger v1 examples/jobs/minimal
```

**4. Try it without DMS.** A dry run runs every job with `ORCH_DRY_RUN=1` (`psql-run` prints the
SQL instead of running it, built-ins only read) and never starts the task:

```bash
python scripts/orch.py start --project ledger --task core --jobs-version v1 --dry-run --wait
python scripts/orch.py report <runId>        # every job with its status and message
```

Then point `jobs.default_version` of the task at `v1` (Terraform), or pass `--jobs-version` per
run.

## Phases

| Phase | When it runs | Typical jobs |
|---|---|---|
| `pre` | before the task starts (first attempt only) | permission checks, schema diff, create tables |
| `reset` | only when the run decides the target must be reset (see below) | truncate partial tables / all tables |
| `post` | after DMS finished successfully (also in dry runs) | indexes, foreign keys, sequences, row counts, cleanup |
| `on_failure` | after a failure that is not auto-resumed | diagnostics, notifications, rollback |

Jobs in a phase run **one after another** in file order. A failing job (after its
retries) stops the phase and sends the run to the failure branch, unless the job
says `onError: continue` or `warn`.

### When does `reset` run?

`inspect_task` decides, and puts the tables into `ctx.resetTables`:

| Situation | Start type | Tables to reset |
|---|---|---|
| first run of the task | `start-replication` | none |
| previous run interrupted, policy `if-interrupted` (default) | `resume-processing` | tables not completed (`Full load`, `Table error`, `Table cancelled`, `Before load`, `Table is being reloaded`) |
| previous run interrupted, policy `always` | `reload-target` | **all** tables of the task |
| explicit `startType: reload-target` | `reload-target` | **all** tables (policy `never` rejects the run) |
| policy `never` | `resume-processing` | none |

Why: the upstream DMS module keeps `TargetTablePrepMode = DO_NOTHING`. `resume-processing`
keeps completed tables and reloads the rest; `reload-target` reloads every table.
Without a reset either would duplicate rows. If tables must be reset and the job
set has no `reset` phase, the run is rejected (`ResetNotConfigured`).

## File layout and versions

```
my-bundle/
├── jobset.yaml            # job set "default"
├── jobset.cutover.yaml    # optional named job set (request field jobSet: cutover)
├── ddl/*.sql
└── scripts/*.py | *.sh | *.ps1
```

`scripts/sync-jobs.sh <project> <version> <bundle-dir>` validates the bundle
(`scripts/jobset_tool.py`), converts YAML to `jobset.json` and uploads it to
`s3://<artifacts>/jobs/<project>/<version>/`. Versions are immutable (use the git
SHA). A run uses `jobsVersion` from the request, or the task's
`jobs.default_version` from Terraform. No version → no jobs.

## Job fields

```yaml
schemaVersion: 1
defaults:                     # applied to every job unless the job overrides them
  runtime: fargate            # lambda | fargate (lambda when handler/function is set)
  image: sql-runner           # key of the module variable fargate.images
  timeoutSeconds: 3600        # lambda jobs are capped at 840
  retry: { maxAttempts: 1, backoffSeconds: 60 }
  idempotent: true            # false => never retried automatically
  onError: fail               # fail | continue | warn
phases:
  pre:
    - name: schema-diff                         # unique within the phase: [a-z0-9][a-z0-9_-]*
      command: [python3, scripts/diff_schema.py, --source, source, --target, target]
      when: "ctx.migrationType == 'full-load'"  # optional condition
      artifacts: { upload: [/out/diff.json] }   # files to keep in S3
    - name: grants
      handler: builtin:checks/postgresql_source_grants   # built-in Lambda job
      with: { connection: dms_source, schemas: [app], require: [SELECT] }
    - name: custom
      function: arn:aws:lambda:eu-west-2:111122223333:function:ledger-check  # must be in job_lambda_arns
```

JSON Schema: [`src/shared/schemas/jobset.schema.json`](../src/shared/schemas/jobset.schema.json).

## Conditions and templates

`when:` and `{{ ... }}` in `command` / `with` use a small safe expression language
(no function calls): paths, `== != > >= < <=`, `and or not`, `in`, `not in`,
literals, lists. Missing paths are `null`.

| Name | Content |
|---|---|
| `ctx` | `project`, `taskKey`, `environment`, `taskArn`, `migrationType`, `startType`, `resetScope`, `resetTables`, `connections`, `jobsVersion`, `attempt`; in `post` also `outcome` |
| `run` | `runId`, `dryRun`, `phase` |
| `failure` | in `on_failure`: `category`, `retryable`, `reason`, `lastFailureMessage`, `erroredTables` |
| `pre`, `reset`, `post` | results of earlier jobs of this run: `pre.<job>.status`, `pre.<job>.summary.<field>` |

Examples: `when: "pre.schema-diff.summary.missingTables > 0"`,
`when: "failure.category != 'TRANSIENT_SOURCE_CONN'"`,
`command: [psql-run, --connection, target, --truncate, "{{ ctx.resetTables }}"]`
(lists become comma-separated in command arguments).

## Connections and secrets

Connections are registered in Terraform per task (`projects.<p>.tasks.<t>.connections`)
with engine, host, port, database and **the ARN** of a Secrets Manager secret. Values
never appear in Terraform state, SSM, Step Functions payloads or logs.

* Fargate jobs: `orch_job.py` reads the secrets with the task role and writes
  `$ORCH_CONNECTIONS_FILE` (mode 0600, deleted after the job). `psql-run` and
  `scripts/pgq.py` use it; the password reaches `psql` only through its own environment.
* Built-in Lambda jobs: RDS Data API with the secret ARN (`dataApiClusterArn` on the connection).
* Every secret must be listed in `job_secret_arns` (IAM allow-list).
* Use dedicated users (`migration_runner`, the DMS user), not the master user.

## Fargate jobs (`sql-runner` image)

Entry point `orch_job.py` runs your `command` in the bundle directory.

| Environment | Meaning |
|---|---|
| `ORCH_JOB_SPEC_FILE` | full job spec (JSON): rendered `with`, `ctx`, `failure`, earlier results |
| `ORCH_CONNECTIONS_FILE` | resolved connections (0600) |
| `ORCH_OUT_DIR` (`/out`) | write `result.json` and artifacts here |
| `ORCH_WORK_DIR` | the bundle (current directory) |
| `ORCH_DRY_RUN` | `1` in dry runs: **change nothing**, print what you would do |
| `ORCH_RUN_ID`, `ORCH_PHASE`, `ORCH_JOB_NAME`, `ORCH_ATTEMPT` | run info |

Tools: `psql` 17, `mysql`, `python3` (+ boto3), `bash`, `pwsh`, and
`psql-run` / `mysql-run`:

```
psql-run --connection target --file ddl/020_indexes.sql [--file ...] [--single-transaction]
psql-run --connection target --template sql/reset.sql --var schema=app   # see "SQL templates"
psql-run --connection target --truncate "{{ ctx.resetTables }}" [--cascade]
psql-run --connection target --sql "ANALYZE app.accounts"
```

### SQL templates (`--template`)

Values never go into SQL as raw text:

| Placeholder | Becomes | Example |
|---|---|---|
| `{{ expr }}` | SQL literal: `'text'` with quotes escaped (and backslashes on MySQL), numbers, `TRUE`/`FALSE`, `NULL`; a list → `'a', 'b'` | `WHERE run_id = {{ run.runId }}`, `IN ({{ ctx.resetTables }})` |
| `{{ ident:expr }}` | quoted identifier (`"x"` / `` `x` ``), `schema.table` allowed, a list → `"a"."b", "c"`; anything else fails the job | `TRUNCATE {{ ident:ctx.resetTables }}`, `SET search_path = {{ ident:var.schema }}` |

Do not wrap placeholders in quotes yourself (`'{{ x }}'` would quote twice). There is
no raw form: build dynamic SQL in a script if you really need it.

Root file system is read-only; `/tmp`, `/work` and `/out` are writable. The
container has no inbound access and reaches databases through the jobs security group.

## Result

Every job ends with a result (Lambda: return value; Fargate: `/out/result.json`):

```json
{ "status": "ok | warn | fail", "message": "short text", "summary": { "any": "small JSON" }, "artifacts": ["s3://..."] }
```

* Fargate: a non-zero exit code is `fail` whatever `result.json` says; no
  `result.json` + exit 0 = `ok`. Keep `summary` small (< 16 KB): later jobs read it
  in `when:` conditions and it is stored in DynamoDB and the report.
* Command output (stdout, stderr) goes to the CloudWatch log only, never into the
  result. A failed Fargate job's message is `exit code N (see CloudWatch log stream
  <name>)`, followed by `: <message>` from `result.json` if the job wrote one. The
  message reaches the report and the execution `Cause`, which operators can read:
  never put connection details or row data into `message` or `summary`.
* `warn` lets the run continue and shows up in the report.
* `skipped` is set by the orchestrator when `when:` is false.

Schema: [`src/shared/schemas/job_result.schema.json`](../src/shared/schemas/job_result.schema.json).

## Retries and idempotency

* `retry.maxAttempts` > 1 retries a failed job after `backoffSeconds`, only when
  `idempotent: true` (the default). Write jobs so they can run twice:
  `CREATE ... IF NOT EXISTS`, `DO $$ IF NOT EXISTS ... $$`, `TRUNCATE`.
* `idempotent: false` jobs never retry automatically; `jobset_tool.py` rejects a
  non-idempotent job with `maxAttempts > 1`.
* An auto-resumed run (transient DMS failure) does **not** repeat `pre` jobs.

## Built-in Lambda jobs

| Handler | `with` | Checks / does |
|---|---|---|
| `builtin:checks/connections` | `test` (default `true`), `timeoutSeconds` (300) | endpoint connections are `successful`; untested or failed ones get `TestConnection` first (not in a dry run) |
| `builtin:checks/postgresql_source_grants` | `connection`, `schemas`, `require: [SELECT, REPLICATION]`, `walLevel`, `minFreeSlots` | the user can read the schemas; for CDC: replication role, `wal_level`, free slots |
| `builtin:checks/row_counts` | `source`, `target`, `tables` (default: all task tables), `tolerance` | exact `count(*)` per table (Data API, small/medium tables; use a Fargate job for big ones) |
| `builtin:diagnostics/dms_dump` | — | task, unfinished tables and last 24 h of DMS events → `dms-dump.json` artifact |
| `builtin:dump/cluster_snapshot` | `connection` (`target`), `waitSeconds` (600) | dump phase, `dump.method = snapshot`: tagged cluster snapshot of the connection's cluster, polled until available, `runs/<runId>/dump.json` (ADR 0007) |
| `builtin:cleanup/cluster_snapshots` | `connection`, `retentionDays` | post job `snapshot-retention`: deletes this task's tagged snapshots older than the retention; dry run only lists them |
| `builtin:cleanup/drop_control_tables` | `connection`, `schema` (`dms_control`), `keepValidationFailures` | drops DMS control tables; dry run only lists them |

SQL built-ins need the RDS Data API on the cluster (`enable_data_api` in the upstream
aurora module) and the endpoint `rds-data` in an isolated VPC. Without it, use Fargate jobs.

## Dry run

`dryRun: true` runs `resolve_context`, the lock, `inspect_task` and **all jobs with
`ORCH_DRY_RUN=1`**, but never calls `StartReplicationTask`. Jobs must not change
anything in dry runs: `psql-run` prints the SQL instead of running it, built-in
checks are read-only, cleanup lists only.

Fargate jobs also get `PGOPTIONS=-c default_transaction_read_only=on` (appended to
any `PGOPTIONS` already set) for the whole command environment:

* **Enforced for libpq clients** started by the command (`psql`, `pg_dump`, drivers
  built on libpq): every session starts read-only and a write fails with
  `cannot execute ... in a read-only transaction`. A guard against accidental writes,
  not a security boundary: SQL that runs `SET default_transaction_read_only = off`
  or `SET TRANSACTION READ WRITE` can still write.
* **Advisory for python and pwsh scripts** that open their own sessions through
  other drivers, and for `mysql`: they must check `ORCH_DRY_RUN` themselves.
