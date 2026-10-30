# Job contract: pre, reset, post and on_failure jobs

A **job set** describes what runs around the DMS task. Project teams own it; the
orchestrator runs it. Start with the tutorial below; the rest of this page is the contract.
Examples: [`examples/jobs/minimal`](../examples/jobs/minimal) (the tutorial) and
[`examples/jobs/postgres-basic`](../examples/jobs/postgres-basic) (schema check, DDL, row counts).

## Authoring a first job set

Ten minutes from an empty directory to a job set a run uses.

**1. The structure.** A bundle is a directory with `jobset.yaml` and the files its jobs name:

```
my-jobs/
├── jobset.yaml
└── sql/indexes.sql
```

**2. The job set.** Four phases; each job is a built-in Lambda job (`handler:`), SQL in the
Lambda SQL runtime (`sql:`), or a command in a command runtime (`command:`). This is
[`examples/jobs/minimal/jobset.yaml`](../examples/jobs/minimal/jobset.yaml):

```yaml
# A first job set: check the DMS connections, empty partly loaded tables before a resume,
# build the indexes after the load and compare row counts.
#
# Connections (registered in Terraform, projects.<p>.tasks.<t>.connections): source and
# target with host and secret_arn. Every job runs in Lambda: sql jobs and the SQL built-ins
# reach the databases through pg8000 with TLS verify-full; no image is needed.
#
# Validate: uv run python scripts/jobset_tool.py validate examples/jobs/minimal
# Upload:   scripts/sync-jobs.sh <project> <version> examples/jobs/minimal
schemaVersion: 1
description: connection check, reset of partial tables, indexes after the load, row counts

phases:
  pre:
    - name: check-connections
      handler: builtin:checks/connections   # a built-in Lambda job: TestConnection where needed

  reset:
    - name: truncate-tables
      sql: { connection: target, truncateScope: true }   # ctx.resetTables of the spec file, any size

  post:
    - name: indexes
      sql: { connection: target, file: sql/indexes.sql }
      when: "not run.dryRun"                # it writes; a dry run session is read-only

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
  ([When does `reset` run?](#when-does-reset-run)); `truncateScope` empties exactly the
  tables it decided on (`ctx.resetTables`).
* `post` runs after a successful load; `on_failure` after a failure that is not resumed.
* **Runtime.** `handler:` and `sql:` jobs run in Lambda (up to 840 s; pg8000 with TLS
  verify-full when the connection has a host, the Data API otherwise). A job with `command:`
  runs in a command runtime: `runtime: ssm` on an SSM runner instance from the team AMI
  (psql, pg_dump, pg_restore, bash, python3; pwsh optional; module input `ssm`, RUNBOOK
  section 16), or Fargate in the image named by `image` (psql, mysql, python, bash, pwsh),
  optional ([ADR 0011](adr/0011-job-runtimes.md)). Both run the same wrapper (orch-job) with
  the project's job role, the same environment and the same result contract.
* **Conditions.** Add `when: "<expression>"` to run a job only sometimes, for example
  `when: "ctx.startType == 'reload-target'"` ([Conditions and templates](#conditions-and-templates)).
* **SQL.** `sql: {file: ...}` runs files from the bundle, each file as one transaction;
  placeholders are filled safely ([SQL jobs](#sql-jobs-lambda-sql-runtime)). Write SQL that
  can run twice (`IF NOT EXISTS`): a job may be retried.

**3. Validate, upload and pin.** The tool checks the schema, the job names and that every file a
job names exists; versions in S3 are immutable (use a git sha or tag). `sync-jobs.sh` prints
the bundle hash; a run takes the version only once the hash is in Terraform:

```bash
uv run python scripts/jobset_tool.py validate examples/jobs/minimal
scripts/sync-jobs.sh ledger v1 examples/jobs/minimal
```

```hcl
jobs = { bundles = { v1 = "<bundle sha256 printed by sync-jobs.sh>" } }
```

**4. Try it without DMS.** A dry run runs every job in dry mode (sql jobs in read-only
sessions, `truncateScope` only counts, built-ins only read; `indexes` is skipped by its `when`)
and never starts the task:

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
(`scripts/jobset_tool.py`), converts YAML to `jobset.json`, writes `bundle.sha256` and uploads
it to `s3://<artifacts>/jobs/<project>/<version>/`. A run uses `jobsVersion` from the request,
or the task's `jobs.default_version` from Terraform. No version → no jobs.

**Versions are immutable and pinned.** The upload creates every object with `If-None-Match`
(the publisher policy allows nothing else), so a version is never overwritten: a fix is a new
version (use the git SHA). `bundle.sha256` lists the SHA-256 of every file, sorted by path
(`<sha256>  <path>`), and ends with `total <sha256>`, the hash over those lines. The script
prints the total, and `scripts/jobset_tool.py hash <bundle-dir>` gives the same value from the
source. Terraform registers it per version:

```hcl
jobs = {
  default_version = "3f2c1ab"
  bundles         = { "3f2c1ab" = "<total>" }   # version => bundle hash
}
```

Each run checks the job set against that hash:

| Where | Check | Error |
|---|---|---|
| `ResolveContext` | the version is in `jobs.bundles` | `JobSetNotPinned` |
| `ResolveContext` | `bundle.sha256` exists, its total is the registered one, `jobset.json` matches | `JobSetTampered` |
| Fargate jobs (`sql-runner`) | every downloaded file matches, no file outside the list, none missing; the command does not start otherwise | `JobSetTampered` (job result) |
| built-in Lambda jobs, sql jobs | every bundle file they read matches; a sql job opens no session otherwise | `JobSetTampered` |

The run context keeps the hashes (`ctx.jobsBundle`: `sha256`, `files`), and each job carries
`bundleSha256` to its runtime, so a file changed after the run started is refused too.
`jobs.allow_unpinned = true` (sandbox, development) lets versions without a hash run; files are
still checked against a `bundle.sha256` uploaded with them, and a registered hash always wins.

## Job fields

```yaml
schemaVersion: 1
defaults:                     # applied to every job unless the job overrides them
  runtime: fargate            # lambda | ssm | fargate (lambda when handler/function/sql is set)
  image: sql-runner           # key of the module variable fargate.images
  timeoutSeconds: 3600        # lambda jobs are capped at 840
  retry: { maxAttempts: 1, backoffSeconds: 60 }
  idempotent: true            # false => never retried automatically
  onError: fail               # fail | continue | warn
phases:
  pre:
    - name: schema-check                        # unique within the phase: [a-z0-9][a-z0-9_-]*
      sql: { connection: target, file: sql/check.sql, vars: { schema: app } }  # Lambda SQL runtime
      when: "ctx.migrationType == 'full-load'"  # optional condition
    - name: schema-diff
      command: [python3, scripts/diff_schema.py, --source, source, --target, target]  # command runtime
      artifacts: { upload: [/out/diff.json] }   # files to keep in S3
    - name: grants
      handler: builtin:checks/postgresql_source_grants   # built-in Lambda job
      with: { connection: dms_source, schemas: [app], require: [SELECT] }
    - name: custom
      function: arn:aws:lambda:eu-west-2:111122223333:function:ledger-check  # must be in job_lambda_arns
```

JSON Schema: [`modules/dms-orchestration/src/shared/schemas/jobset.schema.json`](../modules/dms-orchestration/src/shared/schemas/jobset.schema.json).
`runtime: ssm` jobs need the module's SSM runtime (`ssm.enabled`); a module without it refuses
them when they are prepared (`InvalidJobSet`). On a runner the command comes from `spec.json`,
never from the SSM document, and runs as `ssm.runner_user` with the AMI's `python3` (3.9 or
later).

## SQL jobs (Lambda SQL runtime)

A job with `sql:` runs in the module's `sql_jobs` Lambda (`lambda.sql_jobs_enabled`, on by
default): pg8000, pure Python and vendored in the Lambda zip with hashed requirements, TLS
`verify-full` (or `verify-ca`) against the RDS CA bundle shipped in the zip; any other
`sslMode` is refused. It logs in as the project job role (the connection's secret, or an IAM
token for `auth = iam`) and needs a connection with `host`; PostgreSQL only.

| Field | Meaning |
|---|---|
| `connection` | registered connection (default `target`) |
| `file` | one bundle file or a list (up to 20), checked against `bundle.sha256` |
| `inline` | SQL in the job set itself (up to 16 KB) |
| `truncateScope: true` | `TRUNCATE` of `ctx.resetTables` in one statement, the full list also above the payload cap; `cascade: true` adds `CASCADE` |
| `vars` | values for `{{ var.<name> }}`; they may use `{{ }}` templates themselves |
| `singleTransaction` | all files in one transaction (`BEGIN` … `COMMIT`, `ROLLBACK` on the first error) |

Exactly one of `file`, `inline` and `truncateScope` is set.

**Transactions.** Each file (and `inline`) is sent to PostgreSQL as **one message**, so it runs
as **one implicit transaction**: all its statements apply, or none. This differs from `psql`,
which commits statement by statement. Statements that cannot run inside a transaction block
(`CREATE INDEX CONCURRENTLY`, `VACUUM`, `CREATE DATABASE`) must be alone in their file.
`singleTransaction: true` puts every file of the job into one transaction; without it each
file commits on its own, in order. psql meta-commands (`\set`, `\i`) do not exist here.

**Templates.** `{{ expr }}` in the SQL becomes a SQL literal and `{{ ident:expr }}` a quoted
identifier, exactly as in [SQL templates](#sql-templates---template) (PostgreSQL quoting);
the scope is `ctx`, `run`, `failure`, `var` and earlier results. Nothing is spliced as raw text.

**Dry run.** The session starts with `default_transaction_read_only = on`: reading SQL runs and
a write fails the job with `ReadOnlySqlTransaction`. Guard jobs that write with
`when: "not run.dryRun"`. SQL that mentions `transaction_read_only` or `READ WRITE` is refused
in a dry run before it is sent. `truncateScope` opens the session and only counts.

**Limits.** 840 s per job (`statement_timeout` is the job timeout less 30 s); longer DDL, dumps
and scripts belong to a command runtime. `summary.result` is the first row of the last
statement (up to 50 columns, values cut at 256 characters) — return counts and flags there,
for `when:` conditions such as `pre.schema-check.summary.result.missing_tables > 0`, never row
data. The result also goes to `result.json` next to the job's `spec.json`.

## Conditions and templates

`when:` and `{{ ... }}` in `command` / `with` / `sql.vars` use a small safe expression language
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
`command: [psql-run, --connection, target, --truncate, "{{ failure.erroredTables }}"]`
(lists become comma-separated in command arguments; a reset list kept in S3 above the
payload cap is refused as a template — use `sql: {truncateScope: true}` or `--truncate-scope`).

## Connections and secrets

Connections are registered in Terraform per task (`projects.<p>.tasks.<t>.connections`)
with engine, host, port, database and **the ARN** of a Secrets Manager secret. Values
never appear in Terraform state, SSM, Step Functions payloads or logs.

* Fargate jobs: `orch_job.py` reads the secrets with the task role and writes
  `$ORCH_CONNECTIONS_FILE` (mode 0600, deleted after the job). `psql-run` and
  `scripts/pgq.py` use it; the password reaches `psql` only through its own environment.
* sql jobs and SQL built-in Lambda jobs: pg8000 when the connection has a `host` (secret or IAM
  token read as the project job role, kept in memory only), otherwise the RDS Data API with the
  secret ARN (`dataApiClusterArn` on the connection).
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
psql-run --connection target --truncate-scope [--cascade]   # ctx.resetTables from the job spec
psql-run --connection target --truncate "app.a,app.b" [--cascade]
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

Schema: [`modules/dms-orchestration/src/shared/schemas/job_result.schema.json`](../modules/dms-orchestration/src/shared/schemas/job_result.schema.json).

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
| `builtin:checks/row_counts` | `source`, `target`, `tables` (default: all task tables), `tolerance` | exact `count(*)` per table (small/medium tables; use a command runtime for big ones) |
| `builtin:diagnostics/dms_dump` | — | task, unfinished tables and last 24 h of DMS events → `dms-dump.json` artifact |
| `builtin:dump/cluster_snapshot` | `connection` (`target`), `waitSeconds` (600) | dump phase, `dump.method = snapshot`: tagged cluster snapshot of the connection's cluster, polled until available, `runs/<runId>/dump.json` (ADR 0007) |
| `builtin:cleanup/cluster_snapshots` | `connection`, `retentionDays` | post job `snapshot-retention`: deletes this task's tagged snapshots older than the retention; dry run only lists them |
| `builtin:cleanup/drop_control_tables` | `connection`, `schema` (`dms_control`), `keepValidationFailures` | drops DMS control tables; dry run only lists them |

SQL built-ins (`postgresql_source_grants`, `row_counts`, `target_verify`,
`drop_control_tables`) reach a connection with a `host` through pg8000 (the jobs security
group needs the database port, like any job) and one without a host through the RDS Data API
(`enable_data_api` in the upstream aurora module, endpoint `rds-data` in an isolated VPC).
Both give the same results; the pre-flight fingerprint and its hash are identical.

## Dry run

`dryRun: true` runs `resolve_context`, the lock, `inspect_task` and **all jobs in dry
mode** (`ORCH_DRY_RUN=1` for commands), but never calls `StartReplicationTask`. Jobs must not
change anything in dry runs: sql jobs run in read-only sessions (writes fail with
`ReadOnlySqlTransaction`, `truncateScope` only counts), `psql-run` prints the SQL instead of
running it, built-in checks are read-only, cleanup lists only.

* **Enforced in the Lambda SQL runtime and the SQL built-ins**: the pg8000 session starts with
  `default_transaction_read_only = on`, and a sql job whose SQL mentions
  `transaction_read_only` or `READ WRITE` is refused before it is sent.

Command jobs also get `PGOPTIONS=-c default_transaction_read_only=on` (appended to
any `PGOPTIONS` already set) for the whole command environment:

* **Enforced for libpq clients** started by the command (`psql`, `pg_dump`, drivers
  built on libpq): every session starts read-only and a write fails with
  `cannot execute ... in a read-only transaction`. A guard against accidental writes,
  not a security boundary: SQL that runs `SET default_transaction_read_only = off`
  or `SET TRANSACTION READ WRITE` can still write.
* **Advisory for python and pwsh scripts** that open their own sessions through
  other drivers, and for `mysql`: they must check `ORCH_DRY_RUN` themselves.
