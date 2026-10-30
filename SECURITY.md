# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.2.x | yes |
| 0.1.x | no: 0.2.0 fixes security findings in it; upgrade |

## Reporting a vulnerability

Please do not open a public issue, pull request or discussion for a vulnerability.

Report it privately through GitHub's **private vulnerability reporting**: on the repository
page open *Security* → *Report a vulnerability* (the form at `/security/advisories/new`). The
report is visible only to the maintainers. Include:

* what is affected (module input, state machine, Lambda, CLI, the `sql-runner` image) and the
  version or commit;
* how to reproduce it, ideally with `terraform test` or a pytest case instead of a live account;
* the impact you see (for example a permission wider than the trust model in
  [docs/security.md](docs/security.md), a secret value reaching state, logs or a payload).

Never include real credentials, account ids or data in a report; synthetic values are enough.

## What happens next

The maintainers acknowledge the report in the advisory, agree on the scope, prepare a fix on a
private branch and publish a GitHub security advisory with the fixed version. Credit is given in
the advisory if you want it.

## Scope

The code in this repository: the Terraform module, the state machines, the Lambda and job code,
the scripts and the sandbox example. The upstream aurora/dms modules, AWS services and the
container base images are out of scope; report problems there to their maintainers.
