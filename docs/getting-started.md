# Getting started: the first deployment and the first run

From an empty migration workspace to a finished full load and its report. Every command here
is in the repository (`scripts/orch.py`, `scripts/sync-jobs.sh`, `scripts/jobset_tool.py`);
the operator side of the same steps, with every outcome and failure, is in
[`RUNBOOK.md`](../RUNBOOK.md) §1–3. To try it end to end in a throw-away AWS account first, use
the sandbox: [`examples/sandbox/README.md`](../examples/sandbox/README.md).

## 1. Prerequisites

| What | Why |
|---|---|
| Terraform 1.9.8 (any `>= 1.9, < 2`), provider `hashicorp/aws >= 5.90, < 6`, `hashicorp/archive ~> 2.4` | the module avoids 1.10+ features (`modules/dms-orchestration/versions.tf`) |
| A DMS replication instance, endpoints and tasks created with `start = false` | the orchestrator starts tasks, Terraform never does; any module that satisfies [the upstream modules contract](upstream-modules-contract.md) works |
| Source and target databases (Aurora PostgreSQL or PostgreSQL), each with a Secrets Manager secret holding `host`, `port`, `username`, `password`, `dbname` | jobs and pre-flight read connections through secret ARNs only |
| The RDS Data API on the target cluster, or a `host` in the target connection | pre-flight's target check and the built-in SQL checks run in a Lambda, through the Data API or pg8000 over verified TLS (`preflight.verifyRuntime`, [preflight.md](preflight.md)) |
| Private subnets (at least two) and the VPC endpoints of the `network_contract` output | Lambdas and job runners run without internet access |
| Optional: SSM-managed instances built from your AMI (`ssm`, [RUNBOOK.md](../RUNBOOK.md) §16) | long jobs: `pg_dump`/`pg_restore` dumps and restores, long DDL, scripts; SQL jobs need none ([ADR 0011](adr/0011-job-runtimes.md)) |
| Optional: a `sql-runner` image you build and push yourself, pinned by digest (`fargate.images`; `modules/dms-orchestration/src/jobs/sql-runner/Dockerfile`) | the optional Fargate runtime; the module never builds an image and creates no ECS resources without it |
| Python 3.13 with `uv` and the AWS CLI v2 on the operator machine | `orch.py`, `sync-jobs.sh` |

## 2. Wire the module

Next to the dms module, in the same workspace, pass its outputs as `dms = {...}` and register
each DMS task under a project. The full example is in the [README](../README.md#use-the-module);
the minimum:

```hcl
module "dms_orchestration" {
  source = "git::ssh://git@github.com/<org>/dms-orchestration.git//modules/dms-orchestration?ref=v0.2.0"

  environment = "dev"
  vpc_id      = var.vpc_id
  subnet_ids  = var.private_subnet_ids

  dms = {
    replication_instance_arn = module.dms.replication_instance_arn
    replication_instance_id  = module.dms.replication_instance_id
    task_arns                = module.dms.task_arns
    endpoint_arns            = module.dms.endpoint_arns
  }

  projects = {
    ledger = { tasks = { core = {
      connections = {
        source = { engine = "postgresql", secret_arn = var.source_secret_arn,
                   data_api_cluster_arn = var.source_cluster_arn }
        target = { engine = "postgresql", secret_arn = var.target_secret_arn,
                   data_api_cluster_arn = var.target_cluster_arn }
      }
      # bundles: the hash sync-jobs.sh prints for v1 (step 4); runs refuse an unpinned version
      jobs = { default_version = "v1", bundles = { v1 = var.jobs_v1_sha256 } }
    } } }
  }

  job_secret_arns = [var.source_secret_arn, var.target_secret_arn]
}
```

Every field is explained in the [configuration reference](configuration.md). Outside a sandbox
also pass `sns_topic_arn`, `table_name` and `artifacts_bucket_name` from a long-lived workspace,
so locks, runs and baselines outlive the DMS workspace ([`RUNBOOK.md`](../RUNBOOK.md) §10).
The brought bucket must have the controls of the `bucket_requirements` output: versioning
(required: `post` loads the run context by the version `migrate` recorded), SSE-KMS default
encryption, TLS only, SSE-KMS only under `dumps/`, and the lifecycle of `runs/`, `jobs/` and
`dumps/`. The `modules/artifacts-bucket` module creates such a bucket in the long-lived workspace:

```hcl
module "artifacts_bucket" {
  source = "git::ssh://git@github.com/<org>/dms-orchestration.git//modules/artifacts-bucket?ref=v0.2.0"

  name        = "dms-dev-artifacts-<account-id>"
  kms_key_arn = var.kms_key_arn
}
# in the DMS workspace: artifacts_bucket_name = <its bucket_name output>
```

Let the jobs reach the databases: add the `jobs_security_group_id` output to the ingress of both
clusters (with the upstream aurora module: `ingress_rules = { orch = { referenced_security_group_id
= module.dms_orchestration.jobs_security_group_id } }`), or pass `database_security_group_ids`.

## 3. Apply and check the network

```bash
terraform init
terraform apply
terraform output -json dms_orchestration | jq .network_contract     # endpoints the VPC must have
terraform output -json dms_orchestration | jq -r .bucket_requirements | jq .  # controls of a brought bucket
terraform output -json dms_orchestration | jq -r '.verify_commands[]' # a plan run without DMS
```

The commands read a root output `output "dms_orchestration" { value = module.dms_orchestration }`.
`apply` never starts a migration. Attach `operator_policy_json` to the role operators (or the
pipeline) use, and `jobset_publisher_policy_json["<project>"]` to whoever uploads that project's
job sets (`sync-jobs.sh`); the operator policy cannot upload them. The module creates no VPC endpoints, KMS key policies or operator roles; it
publishes what they must allow (`network_contract`, `kms_key_policy_requirements`,
`bucket_requirements`). The `rds` endpoint is in `network_contract` only when a task has
`defaults.snapshot_dumps = true`.

## 4. Upload a job set

A job set says what runs before and after DMS. Start from
[`examples/jobs/minimal`](../examples/jobs/minimal) (the tutorial in
[job-contract.md](job-contract.md#authoring-a-first-job-set)) or the fuller
[`examples/jobs/postgres-basic`](../examples/jobs/postgres-basic):

```bash
export ORCH_STATE_MACHINE_ARN=$(terraform output -json dms_orchestration | jq -r .state_machine_arns.migrate)
export ORCH_TABLE=$(terraform output -json dms_orchestration | jq -r .table_name)
export ORCH_BUCKET=$(terraform output -json dms_orchestration | jq -r .artifacts_bucket)

uv run python scripts/jobset_tool.py validate examples/jobs/minimal
scripts/sync-jobs.sh ledger v1 examples/jobs/minimal
```

Versions are immutable; use a git sha or tag. The version named in `jobs.default_version` (or in
the request as `jobsVersion`) is what a run uses, and only once it is pinned: `sync-jobs.sh`
prints `bundle sha256: <hash>` (`scripts/jobset_tool.py hash examples/jobs/minimal` gives the
same value before the upload); put it into `jobs.bundles` of the task and apply. Runs refuse a
version without a hash (`JobSetNotPinned`) and a bundle that differs from it (`JobSetTampered`).
A sandbox can set `jobs.allow_unpinned = true` ([job contract](job-contract.md#file-layout-and-versions)).

## 5. Plan: what would happen

```bash
python scripts/orch.py start --project ledger --task core --operation plan --wait
python scripts/orch.py report <runId>
```

The report shows the pre-flight verdict (`ready` for a first run into an empty target), the
start type, the tables a reset would touch and any warnings. Nothing is started or changed.
Verdicts and what to do about each: [preflight.md](preflight.md).

## 6. Rehearse, then run

```bash
python scripts/orch.py start --project ledger --task core --dry-run --wait     # every job with ORCH_DRY_RUN=1, no DMS start
python scripts/orch.py start --project ledger --task core --wait --change-ref CHG123
python scripts/orch.py status <runId>      # migrate, post and the run item
```

A run is two executions: `migrate <runId>` ends right after the DMS start, the DMS event starts
`post <runId>-post` ([architecture.md](architecture.md)). `--wait` follows both and exits 0 on
success.

## 7. Read the result

```bash
python scripts/orch.py report <runId>      # markdown; --format json for pipelines
python scripts/orch.py gate <runId>        # exit 0: SUCCEEDED and still the latest baseline
```

The report has the outcome, every job with its result, the DMS table statistics, the target
baseline and, on a failure, the category and the cause from the task log. Every status and
category with its next step: [`RUNBOOK.md`](../RUNBOOK.md) §3–4. Symptoms that are not a run
status: [troubleshooting.md](troubleshooting.md).
