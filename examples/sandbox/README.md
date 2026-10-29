# Sandbox: end-to-end runs of the orchestrator

A throw-away stack for the e2e scenarios: VPC, two Aurora PostgreSQL Serverless v2 clusters
(source, target) from the **upstream `aurora` module**, one DMS full-load task from the
**upstream `dms` module** (`start = false`), and the `dms-orchestration` module wired to the
dms module's outputs. Live runs cost money and change a real AWS account: run them only when
approved, and always end with the teardown at the bottom of this page.

## Upstream modules

The upstream modules are not part of this repository. `sbx fetch-upstream` checks out
`modules/aurora` and `modules/dms` of the repository in `UPSTREAM_MODULES_GIT` at the commit
in `UPSTREAM_MODULES_REF` (environment, or `upstream.env` copied from
[`upstream.env.example`](upstream.env.example)) into `./upstream/`, which `main.tf` sources.
`upstream.env` and `./upstream/` are gitignored. Patches in `upstream-patches/*.patch`, if
you add any, are applied after the checkout (none ship with the repository). The contract
lists two known defects of one dms module implementation (2.3, stable `for_each` keys for
secret endpoints, and 2.6, `postgres.max_file_size`); the workarounds below stay harmless
with a module that has them. The
aurora module must support IAM database users (`create_iam_database_users`) for `s1iam`. Any
implementation that satisfies [the contract](../../docs/upstream-modules-contract.md) works;
why the modules are not vendored: [ADR 0010](../../docs/adr/0010-upstream-modules-dependency.md).
`make sandbox-validate` (repository root) fetches them and runs `terraform validate` offline.

## Cost

| Resource | Why it is cheap | ≈ per hour |
|---|---|---|
| VPC, public subnets, no NAT | NAT gateway avoided | 0 |
| 8 interface endpoints, one subnet | Lambdas and DMS reach AWS APIs (incl. `rds-data`) | 0.08 $ |
| 2 × Aurora Serverless v2, 0–2 ACU, auto-pause after 5 min | compute 0 while paused; ≈ 0.06 $/ACU-h when busy | 0–0.25 $ |
| DMS `dms.t3.small` (smallest offered), single AZ, 20 GB | | 0.04 $ |
| Lambda, Step Functions, DynamoDB on demand, SNS, S3 | pay per use | ≈ 0 |

A whole matrix run is in the order of one dollar. Everything carries the tag
`project=dms-orch-sandbox` and the prefix `dms-orch-sbx` (variables `project_tag`, `name`).

## Variables

Give them as `TF_VAR_*` once per shell, so every `terraform` command (and `e2e s15`, which runs
`terraform apply` itself) sees the same configuration:

| Variable | Default | Turns on |
|---|---|---|
| `TF_VAR_operator_cidr` | — (required) | your IP for seeding through the cluster endpoints |
| `TF_VAR_sql_runner` | `false` | ECR, CodeBuild and the Fargate `sql-runner` task definition (`s1f`, `s10`, `s8fargate`, `s1iam`) |
| `TF_VAR_sql_runner_image_tag` | `""` | the image tag `sbx build-image` printed; the task definition and the ECR endpoints come with it |
| `TF_VAR_assessment` | `false` | the premigration assessment bucket and role (`s14`) |
| `TF_VAR_snapshot_dumps` | `false` | job role snapshot rights and the `rds` endpoint (`s10snap`) |
| `TF_VAR_iam_auth` | `false` | IAM database authentication on the target and the user `orch_iam` (`s1iam`) |
| `TF_VAR_sqs_fanout` | `false` | an SQS queue subscribed to the run topic with a filter policy (`s16`) |

## Run

```bash
export AWS_PROFILE=<profile>
cd examples/sandbox
alias sbx='uv run --with pg8000 python scripts/sandbox.py'
alias e2e='uv run --with pg8000 python scripts/e2e.py'

# 0. the upstream modules (upstream.env from upstream.env.example: URL and full commit sha)
sbx fetch-upstream
uv run python ../../scripts/verify_destroyed.py --match dms-orch-sbx --profile $AWS_PROFILE --region us-east-1   # the account is clean

# 1. apply
export TF_VAR_operator_cidr=$(curl -s https://checkip.amazonaws.com)/32
export TF_VAR_sql_runner=true TF_VAR_assessment=true TF_VAR_snapshot_dumps=true TF_VAR_iam_auth=true TF_VAR_sqs_fanout=true
terraform init
terraform apply -target=aws_secretsmanager_secret.db   # secrets first (contract 2.3; harmless with the patch)
terraform apply                                         # if it fails on IAM eventual consistency, run it again
sbx build-image $(git rev-parse --short HEAD)           # CodeBuild + trivy gate, prints the tag
export TF_VAR_sql_runner_image_tag=$(git rev-parse --short HEAD)
terraform apply                                         # task definition + ECR endpoints

# 2. data and job sets
sbx seed              # source: schema + 235k rows + the dms_source password; target: schema + dms_control
sbx test-connections  # target MaxFileSize 0 -> 32768 if the dms module left it at 0, TestConnection of both endpoints
sbx sync-jobs         # job sets "e2e", "e2e-dms-only", "e2e-postfail"
sbx sync-jobs-fargate # job set "e2e-fargate"
sbx sync-jobs-iam     # job set "e2e-iam"
```

Run `sbx test-connections` again after every later `terraform apply`: an apply that touches an
endpoint resets its connection status.

After a change to the image's code, commit, then `terraform apply` (it uploads the build source
to S3), then `sbx build-image <new sha>` and `terraform apply` with the new tag. CodeBuild builds
from the uploaded zip and the tags are immutable: `build-image` refuses while that zip differs
from the working tree.

## Scenarios

What each one proves and when it is required: [docs/testing.md](../../docs/testing.md#end-to-end-matrix).

```bash
e2e                      # the default set: s1 s1p s7 s2 s3 s5 s6 idem s8 s4 s9 s13
e2e s7 s1                # dry run, then a full load
e2e s1p                  # waits for both clusters to auto-pause (up to 30 min), then S1
e2e s2 s3 s5 s6          # interrupt and resume, revoked grant, two runs, lost event
e2e idem                 # StartExecution idempotency by name
e2e s8 s4 s9             # spoiled target -> MANUAL, allowReset, resume through two machines
e2e s13                  # a failed post job, repeated with orch.py post --from-job
e2e s1f                  # reset and post jobs on Fargate (sql_runner)
e2e s1iam                # a Fargate post job logged in by IAM token (iam_auth)
e2e s8fargate            # the target check through psql on Fargate (sql_runner)
e2e idem-sfn             # a real ExecutionAlreadyExists from post's StartResume
e2e s10                  # dump, failed reload, restore, restore baseline (sql_runner)
e2e s10snap              # dump by cluster snapshot, retention (snapshot_dumps)
e2e s14                  # premigration assessment stops before DMS (assessment)
e2e guard                # an armed MIGRATION# refuses before the reset
e2e postdie              # a dead post releases its lock in seconds
e2e s16                  # fan-out: FAILED and MANUAL reach the queue, SUCCEEDED does not (sqs_fanout)
e2e s15                  # replaces the DMS task with terraform apply -replace (needs the TF_VAR_* above)
e2e s1                   # last: the final code is green

(cd ../.. && uv run python tests/sfn/run_teststate.py --region us-east-1)   # TestState cases against AWS
```

`s10snap` deletes its own snapshot; snapshots are not in the state, so check them after the
run with `verify_destroyed.py --match ledger-core` (the snapshot name is
`<project>-<taskKey>-<ts>`). `fanout.tf` subscribes the queue `dms-orch-sbx-fanout` (SSE-SQS)
with the filter policy [`fanout-filter-policy.json`](fanout-filter-policy.json); nothing is
created while `sqs_fanout = false`.

## Teardown

```bash
terraform destroy                       # with the same TF_VAR_* as the apply
terraform state list | wc -l            # 0
uv run python ../../scripts/verify_destroyed.py --match dms-orch-sbx --profile $AWS_PROFILE --region us-east-1
uv run python ../../scripts/verify_destroyed.py --match dms-orch --profile $AWS_PROFILE --region us-east-1
uv run python ../../scripts/verify_destroyed.py --match ledger-core --profile $AWS_PROFILE --region us-east-1
```

Every `verify_destroyed` must print `{}`. `destroy` can wait 20 minutes or more for the Lambda
network interfaces. ECS creates the log group `/aws/ecs/containerinsights/<cluster>/performance`
itself when Container Insights is on (off by default); it is not in the state, `verify_destroyed`
lists it.

If a second `terraform destroy` (after a partial one) fails with `Invalid for_each argument` in
the upstream `dms` module (`endpoints_with_secret` cannot be planned once the secrets are gone,
contract 2.3, without the patch), destroy what is left by address:

```bash
terraform state list | while read -r a; do
  terraform destroy -auto-approve -target="$a"
done
```

Observed behaviour behind these steps: [`docs/dms-observations.md`](../../docs/dms-observations.md),
[`docs/troubleshooting.md`](../../docs/troubleshooting.md).

## Differences from a production setup

* Public subnets; the clusters accept the operator IP for seeding. Not for production.
* Master passwords and the DMS user's password are generated here and kept in local
  state (`allow_plaintext_credentials`); own secrets with host/port/dbname, because DMS
  cannot use the RDS-managed master secret.
* Jobs: built-in DMS checks and Data API checks (`source-grants`, `row-counts-data-api`,
  `drop-control-tables`), plus `lambda/sandbox_sql.py` (pg8000, in the VPC) for the
  reset; the Fargate `sql-runner` image only with `sql_runner = true` (built by CodeBuild
  here, by the platform's image pipeline there).
* `sbx test-connections` raises `MaxFileSize` 0 -> 32768 on the target when the dms module left
  it at 0 (contract 2.6); the orchestrator's pre-check then tests connections.
* The DMS source user is a real non-master user (`dms_source`), so a revoked grant
  (scenario S3) fails inside DMS; jobs use the master user of each database.
* `create_account_service_roles = true` (the account has no `dms-vpc-role`); on the target
  platform the landing zone owns these roles.
* No CMK (`acknowledged_checks = ["no_cmk", ...]`), no alarms or event subscriptions in
  the upstream modules.
