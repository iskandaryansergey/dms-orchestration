# ADR 0003 — JSONata query language in state machines

- Status: accepted
- Date: 2026-10-27

## Context

Step Functions supports two query languages: JSONPath (`InputPath`, `Parameters`,
`ResultSelector`, `ResultPath`, `OutputPath`) and JSONata (`Arguments`, `Output`,
`Assign`, `{% ... %}` expressions, workflow variables).

The task runner needs to carry context across many states (run id, task ARN,
decision, attempt counter, failure details) and to compute small values
(execution names, conditions, string concatenation).

## Decision

Both state machines use `"QueryLanguage": "JSONata"` with workflow variables
(`Assign`). Business decisions (start type, reset, outcome, failure category) stay
in Python Lambdas; JSONata is used only for wiring and simple conditions.

## Consequences

- Fewer fields per state and no `ResultPath` juggling; variables replace the
  ever-growing state payload.
- Variables are limited to 256 KiB each and 10 MiB per execution; we pass only
  ARNs and short summaries, large data goes to S3/DynamoDB.
- Step Functions Local does not support JSONata; state machine behaviour is tested
  with (a) a local path-walker test in `tests/python/test_asl_*.py` that evaluates
  the real JSONata expressions with `jsonata-python`, and (b) the AWS `TestState`
  API cases in `tests/sfn/` once sandbox credentials exist.
- Terraform renders the ASL with `templatefile()`. JSONata uses `{% %}`, which does
  not clash with Terraform's `${}` / `%{}`; the ASL must never contain `${` except
  for template placeholders.
