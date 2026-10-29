# AI-assisted development

Parts of this repository are written with AI coding agents. This page describes how that
work is organised so that any contributor, human or agent, follows the same process. It is
tool-neutral: nothing here depends on a particular assistant. The short operational version
for agents is [`AGENTS.md`](../AGENTS.md).

## Workflow

1. **Plan.** A change starts from a decision record ([`docs/adr/`](adr/)) when it changes
   design, and from a plan split into slices. Each slice states its goal, the files it
   touches, the tests that prove it and the acceptance criteria.
2. **One slice, one branch.** Every slice is developed on its own branch (a separate git
   worktree when several slices run in parallel), and lands as one commit or a short series.
3. **Red test first.** The first change is a test that fails for the reason the slice
   exists: a unit test, a `terraform test` run block, a state machine case in `tests/sfn/cases/`
   or a scenario in the sandbox e2e script.
4. **Gates.** `make lint test` passes locally before every commit; CI runs the same checks
   plus static analysis, the denylist gate, the configuration reference check and the image
   build.
5. **Live sandbox run, only with approval.** Anything that creates or changes cloud
   resources runs only after a human approves it, in a sandbox account, with a budget.
6. **Teardown verification.** Every live run ends with `terraform destroy`, an empty
   `terraform state list` and `scripts/verify_destroyed.py` reporting nothing left behind.

## Guardrails

- **Humans approve every external effect**: cloud changes, pushes, merges, force-pushes,
  history rewrites and messages to anyone. An agent prepares the change and the evidence;
  a person decides.
- **Least privilege.** The module's IAM policies are asserted by `terraform test`; live runs
  use a sandbox account with a budget and guardrail policies, never a production account.
- **No secrets** in the repository, in prompts or in logs. Connections carry secret ARNs,
  never values; gitleaks runs in CI and as a pre-commit hook; tokens are masked in output.
- **Review.** Every change arrives as a pull request with a diff and a green CI. The reviewer
  reads the tests before the code: a test that does not fail without the change proves
  nothing. An agent never approves its own change.
- **Neutral content.** No names of people or organisations, no dates tied to meetings, no
  links to material outside the repository. The denylist gate (`make denylist`) enforces the
  list kept outside the repository.

## Briefing an agent

- Give it the structure, not a conversation: the relevant ADR, the slice of the plan and
  its acceptance criteria.
- Define "done" as evidence: the output of the commands that prove each criterion (tests,
  `terraform validate`, the e2e scenario), not a statement that it works.
- State the boundaries: what must not be touched (the fetched upstream modules in
  `examples/sandbox/upstream/`, production accounts, other branches), which actions need
  approval, and that a refused or failing check is reported, not worked around.
- Keep tasks narrow. One slice per brief; independent slices can run in parallel on
  separate branches.

## Artifacts that help agents

- [`AGENTS.md`](../AGENTS.md): repository map, commands, rules and the pull request checklist.
- ADRs in [`docs/adr/`](adr/): the reasons behind the design, so that a change does not undo
  a decision by accident.
- `docs/testing.md`: the test levels and the e2e matrix.
- The [`Makefile`](../Makefile) as the single entry point for commands; CI runs the same ones.
- State machine cases in `tests/sfn/cases/`: an executable specification of the ASL
  branches (`make teststate-dry` builds the requests offline).
- Schemas in `src/shared/schemas/` and the generated
  [configuration reference](configuration.md): contracts that tests keep in sync with code.
- The denylist gate: a mechanical check instead of a reviewer's memory.

## Attribution

Commits carry no tool attribution trailers: no co-author lines for tools and no session
links. The author and committer are the project's neutral maintainer identity. The denylist
gate rejects session and co-author trailer lines in commit messages (`--history` in CI,
the `commit-msg` pre-commit hook locally).
