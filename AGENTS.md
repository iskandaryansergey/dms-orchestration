# AGENTS.md

Instructions for coding agents (and humans) working in this repository. The process behind
them: [`docs/ai-assisted-development.md`](docs/ai-assisted-development.md).

## Repository map

| Path | What |
|---|---|
| [`modules/dms-orchestration/`](modules/dms-orchestration/) | the Terraform module: state machines, Lambdas, IAM, storage, checks |
| [`modules/dms-orchestration/asl/`](modules/dms-orchestration/asl/) | state machine definitions (ASL, JSONata): `migrate`, `post`, `job-runner` |
| [`modules/dms-orchestration/tests/`](modules/dms-orchestration/tests/) | `terraform test` with a mock provider (no AWS) |
| [`modules/artifacts-bucket/`](modules/artifacts-bucket/) | optional artifacts bucket for a long-lived workspace, with the controls of `bucket_requirements` |
| [`src/lambdas/`](src/lambdas/) | Lambda handlers, one directory each |
| [`src/shared/`](src/shared/) | shared library: store, DMS outcome, pre-flight, job sets, schemas |
| [`src/jobs/sql-runner/`](src/jobs/sql-runner/) | Fargate job image (psql, mysql, python, bash, pwsh) |
| [`scripts/`](scripts/) | operator CLI (`orch.py`), job set tool, teardown check, denylist gate |
| [`examples/sandbox/`](examples/sandbox/) | throw-away stack for live e2e runs |
| [`examples/jobs/`](examples/jobs/) | example job sets |
| [`tests/python/`](tests/python/) | pytest: unit, moto, local ASL runner |
| [`tests/sfn/`](tests/sfn/) | state machine test cases (TestState) |
| [`docs/`](docs/) | architecture, contracts, ADRs, [configuration reference](docs/configuration.md) |
| [`RUNBOOK.md`](RUNBOOK.md) | operating runs |

## Commands

```bash
uv sync                  # Python toolchain and dev dependencies
make lint test           # ruff, terraform fmt, job set validation; pytest, terraform test
make teststate-dry       # build the TestState requests of every state machine case, offline
make denylist            # denied terms in files and paths (list from outside the repository)
make docs                # regenerate the tables in docs/configuration.md
make docs-check          # fail when docs/configuration.md is out of date
make sandbox-validate    # fetch the upstream modules and validate examples/sandbox
```

Commands used by this file: `make lint test`, `make teststate-dry`, `make denylist`,
`make docs-check`, `make sandbox-validate`. `make denylist` needs `DENYLIST_PATTERNS`, a
patterns file outside the repository (`DENYLIST_ARGS="--patterns-file <path>"`) or
`~/.config/dms-orchestration/denylist`. `make sandbox-validate` needs `UPSTREAM_MODULES_GIT`
and `UPSTREAM_MODULES_REF` (see `examples/sandbox/upstream.env.example`).

## Rules

- **Red test first.** Write the failing test, then the change. `make lint test` passes before
  every commit.
- **One commit per item** of the plan, with a message that says what changed and why.
- **No cloud actions without approval.** Do not run `terraform apply`/`destroy`, the e2e
  scenarios, TestState against AWS or any AWS API call unless a human asked for that run.
  After an approved live run: `terraform destroy`, `terraform state list` is empty,
  `scripts/verify_destroyed.py` reports nothing.
- **No pushes, merges or history rewrites** without approval.
- **Do not edit `examples/sandbox/upstream/`**: it is a fetched checkout of the upstream
  modules (gitignored). Local fixes to it go into `examples/sandbox/upstream-patches/*.patch` (not shipped).
- **Neutral content.** No names of people or organisations, no dates tied to meetings, no
  links to material outside the repository, in code, docs, tests, file names and commit
  messages. Commits carry no tool attribution trailers.
- **No secrets** in files, test fixtures, prompts or logs: secret ARNs only.
- Descriptions of module inputs and outputs live in `variables.tf` / `outputs.tf`; run
  `make docs` after changing them.

## Contracts

- Run requests: `src/shared/schemas/run_request.schema.json`.
- Job sets: `src/shared/schemas/jobset.schema.json` ([`docs/job-contract.md`](docs/job-contract.md)).
- State machines: `modules/dms-orchestration/asl/` ([`docs/state-machine.md`](docs/state-machine.md));
  cases in `tests/sfn/cases/`.
- Module inputs and outputs: [`docs/configuration.md`](docs/configuration.md).
- Upstream aurora/dms modules: consumed only through the `dms = {...}` input and the sandbox
  ([ADR 0010](docs/adr/0010-upstream-modules-dependency.md)).

## Pull request checklist

- [ ] The test that proves the change failed before it and passes now.
- [ ] `make lint test` is green; `make teststate-dry` when the ASL changed.
- [ ] `make docs-check` is green; docs updated for changed behaviour.
- [ ] `make denylist` reports nothing in the changed files.
- [ ] A live sandbox run, if AWS behaviour changed, approved and torn down (`verify_destroyed` empty).
- [ ] No secrets, names or external links; no tool attribution trailers in commit messages.
