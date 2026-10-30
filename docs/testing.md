# Testing

What each level proves, how to run it and how to add to it. Everything above the live level
runs offline, without AWS credentials, and is what `make lint test` and CI run.

## Levels

| Level | Tool | Proves | Run |
|---|---|---|---|
| Unit | pytest (`tests/python/`) | decision logic (`src/shared/dms_outcome.py`, `src/shared/preflight.py`, handoff), schemas, job set rules, the CLI with fake clients | `make py-test` |
| Lambdas against fake AWS | pytest + moto | handlers with DynamoDB, S3, SSM, Secrets Manager, RDS snapshots, IAM tokens; the DMS and Step Functions calls through stubs | `make py-test` |
| State machine paths | local ASL runner (`tests/python/asl_runner.py`) | walks the real `migrate`/`post`/`job-runner` definitions and evaluates their real JSONata, state by state | `make py-test` |
| Terraform | `terraform test` with a mock provider (`modules/dms-orchestration/tests/*.tftest.hcl`, `modules/artifacts-bucket/tests/*.tftest.hcl`) | IAM least privilege, per-project roles, validations, advisory checks, the upstream module contract, the bucket controls | `make tf-test` |
| State machine cases | AWS TestState (`tests/sfn/cases/`, `tests/sfn/run_teststate.py`) | every case against the real Step Functions service; `--dry` builds the requests offline | `make teststate-dry`; with credentials `uv run python tests/sfn/run_teststate.py` |
| Image | `docker build` + trivy | the `sql-runner` image builds, starts, and has no fixable HIGH/CRITICAL CVE | `make image`, CI job `image` |
| Content | denylist gate (`scripts/check_denylist.py`) | no denied term in files, paths, commit messages or authors | `make denylist` |
| Documentation | pytest (`test_docs_links.py`, `test_docs_matrix.py`, `test_variables_doc.py`) | links resolve, commands in the docs parse, the configuration reference and this matrix match the code | `make py-test`, `make docs-check` |
| Live | `examples/sandbox/scripts/e2e.py` in the sandbox | the end-to-end behaviour against real DMS, Aurora and Step Functions | only with an approved sandbox run ([sandbox README](../examples/sandbox/README.md)) |

`make lint test` runs ruff, `terraform fmt`, the example job set check, pytest with coverage and
`terraform test`. CI adds gitleaks, tflint, trivy config, checkov, shellcheck, hadolint, the
terraform-docs check, both pinned Terraform versions, the image and, when the repository
variable is set, `make sandbox-validate`.

## End-to-end matrix

`e2e.py` without arguments runs the default set (marked **default**); the other scenarios run
only by name because they need an optional part of the sandbox or plant state. Every scenario
prints a JSON verdict per check; the exit code is 0 only if every check passed.

| Scenario | Proves | Needs | Required when |
|---|---|---|---|
| `s1` | full load from scratch: `SUCCEEDED`, counts match, Data API jobs ok, post started by the DMS event | **default** | every live run, first and last |
| `s1p` | S1 after both clusters auto-paused (0 ACU): pre-flight and the Data API jobs wake them | **default** | changes to timeouts, retries or the Data API jobs |
| `s1f` | S1 with the reset and post jobs in the Fargate `sql-runner` image (psql over TLS) | `sql_runner`, image | changes to Fargate jobs or the image |
| `s1iam` | a Fargate post job logs in with an IAM token, without a secret, over TLS | `iam_auth`, image, `sync-jobs-iam` | changes to IAM database authentication |
| `s1ssm` | S1 with the reset and post jobs on the SSM runner (document, sha256 of the runtime zip, job role assumed by orch-job, psql over TLS) | `ssm_runner`, `sync-jobs-ssm` | changes to the SSM runtime, orch-job or the runner document |
| `s7` | dry run: nothing started, nothing written | **default** | changes to the dry-run path |
| `s2` | a load interrupted from outside resumes without duplicate rows | **default** | changes to resume or reset |
| `s3` | a revoked grant: the pre-check or DMS fails, classified `PERMISSIONS` | **default** | changes to classification or the grants check |
| `s5` | two runs at once: the second is rejected by the lock | **default** | changes to the lock |
| `s6` | the DMS events rule disabled: the watchdog starts post | **default** | changes to the handoff or the watchdog |
| `idem` | `StartExecution` idempotency by name (same and other input, after the end) | **default** | changes to execution names |
| `idem-sfn` | post's `StartResume` meets an execution of the same name: `ExecutionAlreadyExists`, no failure | plants a lock and a decoy execution | changes to the resume handoff |
| `s8` | a spoiled target (extra table, edited rows): `MANUAL` with the diff, the gate refuses | **default** | changes to pre-flight or the gate |
| `s8fargate` | the target check through psql on Fargate gives the same hash as the Data API one, then S8; leaves the target spoiled, so `s4` runs next | image | changes to the fingerprint or `verify-target` |
| `s4` | the same target with `allowReset`: reset, reload, counts match, the gate passes | **default** | changes to the reset verdict |
| `s9` | S2 through two machines: the resume verdict sees the baseline of the interrupted run | **default** | changes to baselines |
| `s13` | a post job fails; `orch.py post --from-job` with a fixed job set skips the jobs already ok | **default** | changes to repeat post |
| `s15` | the DMS task replaced by Terraform between runs: `MANUAL`, then `allowReset` recovers | `TF_VAR_*` of the stack | changes to `TABLE#` copies or registration |
| `s10` | dump before a reload that fails half-way, restore: counts as before, restore baseline; on the SSM runner when the sandbox has one (lock, scoped TOC, manifest check there) | `ssm_runner` and `sync-jobs-ssm`, or image | changes to dumps, restore, baselines or the SSM runtime |
| `s10snap` | `dump.method = snapshot`: tagged cluster snapshot, `dump.json`, retention job | `snapshot_dumps` | changes to snapshot dumps |
| `s14` | premigration assessment with `fail_on_warning`: stops before DMS, target untouched | `assessment` | changes to the assessment step or its order |
| `guard` | an armed `MIGRATION#` of another run refuses before the reset: target untouched | plants a `MIGRATION#` item | changes to the Arm guard |
| `postdie` | a post that dies in `LoadRun` releases its run's lock in seconds | plants task locks | changes to `release_lock` |
| `s16` | SQS fan-out: `FAILED` and `MANUAL` reach the filtered queue, `SUCCEEDED` does not | `sqs_fanout` | changes to notifications or message attributes |

`tests/python/test_docs_matrix.py` fails when this table and `e2e.py` disagree.

## Adding a scenario

1. Write the function in `examples/sandbox/scripts/e2e.py`: start a run with `start(...)`, follow
   it with `wait(...)`, and return `verdict(name, {check: bool, ...})` with one entry per fact
   the scenario proves. Put helpers that need no AWS (parsers, checks over a history) in
   `examples/sandbox/scripts/sandbox.py` and test them in `tests/python/test_sandbox_helper.py`.
2. Register it in `main()`: in the dict for the default set, or `scenarios["name"] = fn` with a
   comment on what it needs.
3. Add a row to the matrix above and a line to the docstring at the top of `e2e.py`.
4. Optional sandbox parts are variables in `examples/sandbox/variables.tf`, off by default, so
   the default stack stays cheap.

## Adding a state machine case

1. Add the path to `tests/sfn/make_cases.py` (state, input, mocked results) and regenerate the
   cases: `uv run python tests/sfn/make_cases.py`.
2. `tests/python/test_sfn_cases.py` runs every case through the local runner;
   `make teststate-dry` builds the AWS requests offline.
3. A behaviour of the machines themselves also gets a path test in `tests/python/test_asl.py`.

## Adding a Terraform assertion

Add a `run` block (or an `assert` in an existing one) to `modules/dms-orchestration/tests/`; the
mock provider needs no credentials. IAM changes always get an assertion: the security model
names the run block that enforces each permission family ([security.md](security.md)).
