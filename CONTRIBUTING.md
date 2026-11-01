# Contributing

Thank you for helping. This page is the cycle every change goes through; the same rules, in
the form a coding agent reads them, are in [AGENTS.md](AGENTS.md), and how work with AI coding
agents is organised here is in [docs/ai-assisted-development.md](docs/ai-assisted-development.md).

## Set up

```bash
git clone <repository> && cd dms-orchestration
uv sync                      # Python 3.13 and the dev tools from uv.lock
make lint test               # must be green on a clean checkout
```

You also need Terraform 1.9.8 on the `PATH` (or `make … TF=<path>`), and for `make docs` /
`make docs-check` the pinned `terraform-docs` (CI installs it; `TFDOCS=<path>` locally). No AWS
credentials are needed for anything above the live level ([docs/testing.md](docs/testing.md)).

## The cycle

1. **Branch** from `main`, one topic per branch.
2. **Red test first.** Write the test that shows the missing behaviour or the bug, and see it
   fail. Where the change has no behaviour (wording, a comment), say so in the pull request.
3. **Change the code**, the smallest change that makes the test pass.
4. **Run the gates**: `make lint test`; also `make teststate-dry` when a state machine changed,
   `make docs` and `make docs-check` when a module input or output changed, and
   `make denylist` (below).
5. **Pull request** with the template; CI runs the same gates plus the security scanners.

## Layout and module conventions

- The Python code the module packages (Lambda handlers, the shared library, the SSM runtime and
  the optional job image) lives under `modules/dms-orchestration/src/`: the module reads nothing
  outside its own directory (`tests/python/test_packaging.py`), so it can be published alone.
- Module inputs follow the registry conventions ([configuration](docs/configuration.md#conventions)):
  every resource behind `create`, `nullable = false` on every variable, `""` rather than `null`
  for an unset optional string, booleans rather than `null` sentinels, `ManagedBy` and `Module`
  tags. A new resource gets its `count`/`for_each` on `create` and a line in
  `conventions.tftest.hcl::create_false_creates_nothing`.

## Commits

- One commit per logical step, the subject in the imperative with a type prefix
  (`feat(preflight): …`, `fix(release-lock): …`, `docs: …`, `test(e2e): …`), the body says
  what changed and why.
- Neutral content: no names of people or organisations, no dates tied to meetings, no links to
  material outside the repository, in code, docs, tests, file names and commit messages.
- No secrets anywhere: connections carry secret ARNs only; test fixtures use synthetic values.
- Commit messages carry no attribution or session trailers.

## The denylist gate

`scripts/check_denylist.py` fails when a denied term appears in tracked files, file paths,
commit messages or commit authors. The list itself never lives in the repository: give it as
`DENYLIST_PATTERNS` (one regex per line), a file outside the checkout
(`make denylist DENYLIST_ARGS="--patterns-file <path>"`) or `~/.config/dms-orchestration/denylist`.
The output names only `path:line` and the pattern number, never the matched text.

CI reads the list from the repository secret `DENYLIST_PATTERNS`. Pull requests from forks do
not receive repository secrets, so the denylist step fails there; a maintainer reviews the
change and runs the gate on a branch of the main repository before merging. The workflow does
not use `pull_request_target` on purpose: it would run untrusted code with the secret.

## Live sandbox runs

Live runs create real AWS resources and cost money. Run them only when a human approved the
run, in a sandbox account, and always end with a verified teardown:

```bash
terraform destroy                      # the same -var / TF_VAR_* as the apply
terraform state list | wc -l           # 0
uv run python scripts/verify_destroyed.py --match <name prefix> --profile <profile> --region <region>
```

`verify_destroyed` must report nothing. The walkthrough and the scenarios:
[examples/sandbox/README.md](examples/sandbox/README.md), [docs/testing.md](docs/testing.md).

## Licence and vulnerabilities

Contributions are accepted under the [MIT licence](LICENSE) of the repository. Do not open a
public issue for a vulnerability; report it privately as described in [SECURITY.md](SECURITY.md).
