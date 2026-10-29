## What and why

<!-- What changes, and the problem it solves. No names of people or organisations, no links to
material outside the repository. -->

## How it was tested

<!-- The commands you ran and their result. -->

## Checklist

- [ ] A red test first: the test that proves the change failed before it and passes now (or: no behaviour changed).
- [ ] `make lint test` is green; `make teststate-dry` when a state machine changed.
- [ ] `make denylist` reports nothing.
- [ ] `make docs` and `make docs-check` when a module input or output changed; docs updated for changed behaviour.
- [ ] A live sandbox run when AWS behaviour changed: approved, the scenarios named above, torn down (`terraform state list` empty, `verify_destroyed` empty).
- [ ] No secrets, account ids or personal data; commit messages without attribution or session trailers.
