# ADR 0010 — Upstream modules as a configurable dependency

Status: accepted

## Context

The `dms-orchestration` module never creates a replication task: it consumes one through the
`dms = {...}` input, which an upstream `dms` module fills from its outputs. The sandbox
(`examples/sandbox`) needs real infrastructure for the end-to-end runs, so it also needs an
upstream `aurora` module (source and target clusters) and an upstream `dms` module.

The sandbox used to reference one concrete modules repository by URL and commit in `source`.
That ties a reusable product to one implementation and puts a third-party identity into the
repository. The options considered:

1. **Vendor minimal copies** of the two modules into `examples/`. The implementation we used
   is MIT-licensed; the licence requires the copyright notice to travel with every copy, and
   that notice names its author. Vendoring is therefore incompatible with a repository that
   names no third party.
2. **A variable in `source`.** Terraform resolves module sources before evaluation: variables
   are not allowed there, and `*_override.tf` files are not documented to override
   `module.source`.
3. **A fixed local path filled by a script.** `source = "./upstream/modules/<name>"`; the
   directory is gitignored and `sandbox.py fetch-upstream` populates it from a repository URL
   and a commit given only in the environment (or a gitignored `upstream.env`).

## Decision

Option 3.

- `examples/sandbox/main.tf` sources `./upstream/modules/aurora` and `./upstream/modules/dms`.
- `sandbox.py fetch-upstream [--ref <sha>]` reads `UPSTREAM_MODULES_GIT` and
  `UPSTREAM_MODULES_REF`, clones sparsely (`modules/aurora`, `modules/dms` only), checks out
  the commit, applies `upstream-patches/*.patch` if any, and records the commit and the
  applied patches in `upstream/.ref`. It refuses an unset URL and anything but a full commit
  sha, and never prints the URL.
- `upstream.env.example` documents the two variables with placeholders.
- The upstream contract (outputs the orchestrator reads, behaviour it relies on) is generic
  and lives in the docs; any implementation that satisfies it can be used.
- CI runs `make sandbox-validate` only when the repository variable `UPSTREAM_MODULES_GIT` is
  set; the other jobs do not depend on the upstream modules.

## Consequences

- The repository contains no URL and no name of an upstream implementation.
- Reproducibility comes from the pinned commit in the environment, not from git.
- Without `fetch-upstream` the sandbox does not initialise; the sandbox README and the
  `sandbox-validate` target make it the first step.
- Fixes to contract defects of one implementation can be carried as patch files instead of
  forks; without them the documented workarounds stay in place.
