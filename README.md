<p align="center"><img src="docs/assets/logo.svg" alt="dms-orchestration" width="600"></p>

# dms-orchestration

Runs AWS DMS replication tasks **outside Terraform**, step by step, with AWS Step Functions.
Terraform (any dms module that satisfies [the contract](docs/upstream-modules-contract.md))
creates the DMS instance, endpoints and tasks with `start = false`; this module adds the
orchestration around them: checks before the start, the start itself, post-work or a failure
branch, a report, and one run per task at a time. `terraform apply` never starts a migration.

```
operator / CI ──► migrate: lock ─ inspect task ─ pre-flight verdict ─ [assessment] ─ pre-jobs
                           ─ [dump] ─ [reset] ─ arm ─ StartReplicationTask ─ end (no waiting)
AWS DMS ──event──► dms_event_handler ─ claim ─► post: post-jobs | classify ─ [auto-resume]
                           ─ on-failure jobs ─ baseline ─ report ─ SNS ─ unlock
jobs:   job-runner ─► built-in Lambda checks | Fargate sql-runner (psql, pg_dump, python, bash, pwsh)
state:  DynamoDB (locks, runs, baselines) · S3 (job sets, reports, dumps) · SSM (registration)
safety: watchdog every 5 min (lost events, dead executions, orphaned locks) · release_lock
```

Start here:

* [Getting started](docs/getting-started.md): the first deployment and the first run.
* [Architecture](docs/architecture.md): one page on components, data and decisions.
* [RUNBOOK.md](RUNBOOK.md): operating runs, every outcome and failure category with its next step.
* [Troubleshooting](docs/troubleshooting.md): symptoms from AWS, their cause and the fix.
* Reference: [configuration](docs/configuration.md) (module inputs and outputs),
  [job contract](docs/job-contract.md), [state machines](docs/state-machine.md),
  [pre-flight](docs/preflight.md), [security](docs/security.md), [ADRs](docs/adr/).
* A throw-away end-to-end stack: [examples/sandbox](examples/sandbox/README.md).

## Use the module

Terraform 1.9.8, `hashicorp/aws >= 5.90, < 6`. In the migration workspace, next to the dms module:

```hcl
module "dms_orchestration" {
  source = "git::ssh://git@github.com/<org>/dms-orchestration.git//modules/dms-orchestration?ref=v0.1.0"

  environment = "dev"
  vpc_id      = var.vpc_id
  subnet_ids  = var.db_tier_subnet_ids          # >= 2 private subnets

  dms = {                                       # outputs of the dms module
    replication_instance_arn = module.dms.replication_instance_arn
    replication_instance_id  = module.dms.replication_instance_id
    task_arns                = module.dms.task_arns
    task_resource_ids        = module.dms.task_resource_ids
    endpoint_arns            = module.dms.endpoint_arns
    log_group_name           = module.dms.log_group_name
  }

  projects = {
    ledger = {
      tasks = {
        core = {                                # taskKey; must exist in dms.task_arns
          connections = {
            source = { engine = "postgresql", secret_arn = var.source_runner_secret_arn,
                       data_api_cluster_arn = var.source_cluster_arn }
            target = { engine = "postgresql", secret_arn = var.target_runner_secret_arn,
                       data_api_cluster_arn = var.target_cluster_arn }
          }
          jobs     = { default_version = "3f2c1ab" }       # job set version in S3
          defaults = { target_reset_policy = "if-interrupted" }
        }
      }
    }
  }

  job_secret_arns             = [var.source_runner_secret_arn, var.target_runner_secret_arn]
  kms_key_arn                 = var.kms_key_arn  # null only in a sandbox
  fargate                     = { images = { sql-runner = "${var.ecr}/dms-orch/sql-runner:${var.jobs_image_tag}" } }
  database_security_group_ids = [var.target_db_security_group_id, var.source_db_security_group_id]
}
```

Every input and output: [docs/configuration.md](docs/configuration.md). Outside a sandbox also
pass `sns_topic_arn`, `table_name` and `artifacts_bucket_name` from a long-lived workspace
(RUNBOOK §9–10). The module creates no VPC endpoints, KMS key policies or operator roles; it
publishes what they must allow (`network_contract`, `kms_key_policy_requirements`,
`operator_policy_json`, `jobset_publisher_policy_json`).

## Run a migration

```bash
scripts/sync-jobs.sh ledger 3f2c1ab examples/jobs/postgres-basic                      # upload a job set version
python scripts/orch.py start --project ledger --task core --operation plan --wait     # what would happen
python scripts/orch.py start --project ledger --task core --dry-run --wait            # jobs in dry mode, no DMS start
python scripts/orch.py start --project ledger --task core --wait --change-ref CHG123  # migrate, DMS, post
python scripts/orch.py report <runId>
python scripts/orch.py gate <runId>                                                   # exit 0: may be promoted
```

Run request fields: [`run_request.schema.json`](src/shared/schemas/run_request.schema.json).
Job sets (pre, reset, post and on-failure jobs, versioned in S3): [docs/job-contract.md](docs/job-contract.md),
examples [minimal](examples/jobs/minimal) and [postgres-basic](examples/jobs/postgres-basic).

## Repository

```
modules/dms-orchestration/   Terraform module (+ asl/ state machines, tests/ terraform test)
src/shared/                  decision logic, pre-flight, handoff (migrate -> post), DynamoDB store, schemas
src/lambdas/                 one directory per Lambda handler
src/jobs/sql-runner/         Fargate image: Dockerfile, orch_job.py entrypoint, psql-run, dumps, verify-target
examples/jobs/               example job sets
examples/sandbox/            throw-away stack and the e2e scenarios
scripts/                     orch.py (CLI), sync-jobs.sh, jobset_tool.py, verify_destroyed.py, check_denylist.py
tests/                       pytest, local ASL runner, TestState cases, e2e seed data
docs/                        getting started, architecture, contracts, troubleshooting, testing, ADRs
```

## Development

```bash
uv sync               # Python 3.13 + dev tools
make lint test        # ruff, terraform fmt, job set checks, pytest, terraform test
make teststate-dry    # build the TestState requests (no AWS)
```

The cycle, the commit rules and the denylist gate: [CONTRIBUTING.md](CONTRIBUTING.md); for coding
agents: [AGENTS.md](AGENTS.md). Test levels and the e2e matrix: [docs/testing.md](docs/testing.md).

## Release 0.1.0

The first release: two state machines with a pre-flight verdict, target dumps by `pg_dump` or
cluster snapshot, IAM database authentication for Fargate jobs, the target check in a Lambda or
on Fargate, SNS fan-out attributes, repeat of post, the sandbox and its e2e matrix. What is in
it: [CHANGELOG.md](CHANGELOG.md).

## License and security

MIT, see [LICENSE](LICENSE). Report vulnerabilities privately as described in
[SECURITY.md](SECURITY.md); the security model is in [docs/security.md](docs/security.md).
