# Security

The architecture stays on one page ([`architecture.md`](architecture.md)); the security model lives here.

## Controls

* IAM role per component, policies built with `jsonencode` and checked by `terraform test`:
  no `*` actions; `*` resources only where AWS has no resource-level permissions
  (`dms:Describe*`, ENI management, log delivery, ECR auth).
  `dms:StartReplicationTask` only on the registered task ARNs; permissions boundary supported.
* No secret values in Terraform, SSM, payloads or logs: connections carry secret ARNs;
  Fargate jobs resolve them into a 0600 file; task tokens are masked in logs.
* `auth = "iam"` connections (ADR 0009) have no secret: `psql-run` signs a 15-minute IAM token
  with the job role at each client start and puts it only into psql's environment (`PGPASSWORD`);
  it is never written to the connections file, an argument, the job result or a log line. TLS is
  required. The job role gets `rds-db:connect` for exactly that database user through the
  upstream aurora module's policy. DMS endpoints and the Data API keep their secrets.
* Cluster snapshots (`dump.method = snapshot`, ADR 0007): the job role creates them only on the
  registered target clusters and with its project tag, and deletes only snapshots with that tag.
* Lambda and Fargate run in private subnets with a security group without ingress;
  databases allow it by security group reference. Its egress is 443 to the VPC CIDR (interface
  endpoints) and the S3/DynamoDB prefix lists (gateway endpoints) — never `0.0.0.0/0`
  (`https_egress_cidr_blocks` / `https_egress_prefix_list_ids` override; a job that needs the
  internet gets an extra group through `security_group_ids`) — plus `database_ports` to the databases.
* Optional customer managed KMS key for DynamoDB, S3, SNS, SQS, logs, Lambda environment,
  Step Functions and the scheduler; `kms_key_policy_requirements` lists what the key policy must allow.
  Without a CMK the SNS topic is unencrypted on purpose: the AWS managed key `alias/aws/sns`
  cannot allow CloudWatch alarms to publish, so alarms would be lost. With a CMK the key policy
  must allow `cloudwatch.amazonaws.com` (listed in `kms_key_policy_requirements`).
* Fargate job containers run as uid 10001 with a read-only root file system and three
  scratch volumes (`/tmp`, `/work`, `/out`). Fargate mounts those empty and root-owned, so a
  non-essential init container of the same image runs as root for about a second to
  `chmod`/`chown` them and exits; it gets no task input, no secrets and no command override,
  and the job container starts only after it succeeded. A platform policy that forbids root
  containers outright would need a sidecar-free alternative (EFS access point with a fixed
  uid, or `VOLUME` directories owned by 10001 if the platform copies them) — open with the
  platform team.
* Isolated VPC: required endpoints are published in `network_contract`.

## Enforced by tests

Every control above has an assertion that fails the build when it is broken. `terraform test`
runs the `.tftest.hcl` files with a mock provider; the Python tests run with moto or fakes.

| Control | Enforced by |
|---|---|
| No wildcard actions; `*` resources only for actions without resource-level permissions | `iam.tftest.hcl::least_privilege` |
| `dms:StartReplicationTask` only on the registered task ARNs | `iam.tftest.hcl::least_privilege` |
| Secrets: orchestration roles read none; a project's job role reads only its own connections' secrets, all in `job_secret_arns` | `iam.tftest.hcl::least_privilege`, `iam.tftest.hcl::per_project_job_roles`, `iam.tftest.hcl::no_secrets_no_data_api_no_fargate`, `validations.tftest.hcl::secret_outside_allow_list` |
| `iam:PassRole` only of the module's roles, only to ECS tasks; the assessment role only to DMS | `iam.tftest.hcl::least_privilege`, `iam.tftest.hcl::assessment_role_is_passed_only_to_dms` |
| Data API only on the registered clusters of the project | `iam.tftest.hcl::least_privilege`, `iam.tftest.hcl::per_project_job_roles` |
| Permissions boundary on every role | `iam.tftest.hcl::least_privilege` |
| Cluster snapshots: create only of the project's target clusters with the project tag, describe and delete only tagged ones | `iam.tftest.hcl::snapshot_dumps_are_scoped_by_the_project_tag`, `test_jobs_lambdas.py::test_snapshot_retention_deletes_only_old_snapshots_of_this_task` |
| `rds-db:connect` only through the upstream aurora module's policy for the connection's user | `iam.tftest.hcl::iam_database_auth_for_fargate_jobs`, `validations.tftest.hcl::iam_connection_without_a_policy_is_refused` |
| The IAM token reaches only psql's environment, never a file, an argument or a log | `test_sql_runner.py::test_iam_token_reaches_psql_only_through_its_environment`, `test_sql_runner.py::test_iam_connection_file_holds_no_password_and_reads_no_secret`, `test_sql_runner.py::test_iam_connection_needs_tls` |
| Task tokens and secrets masked in logs | `test_shared_misc.py::test_log_masks_tokens` |
| Registration in SSM carries secret ARNs, never values | `defaults.tftest.hcl::storage_and_registration` |
| Jobs security group: no ingress; 443 egress only to the VPC CIDR and the gateway prefix lists | `defaults.tftest.hcl::lambda_and_events`, `validations.tftest.hcl::https_egress_to_the_internet`, `validations.tftest.hcl::https_egress_explicit_ranges` |
| Customer managed KMS key on every store and the key policy requirements | `options.tftest.hcl::customer_managed_key` |
| Target dumps: SSE-KMS only, expire after `dump_retention_days` | `defaults.tftest.hcl::dumps_are_kms_only_and_expire` |
| Restore baseline: written only in the `restore` phase, through `dynamodb:LeadingKeys` on `TARGET#` | `iam.tftest.hcl::least_privilege`, `test_jobs_lambdas.py::test_only_the_restore_phase_may_record_a_baseline` |
| Job container: uid 10001, read-only root, the init container gets no input or secrets | `defaults.tftest.hcl::lambda_and_events` |

## Trust model: who can read what

One module instance (one migration workspace per environment) serves many projects.
Projects trust the orchestrator, not each other.

| Principal | Trusted with | Cannot |
|---|---|---|
| Operator role (`operator_policy_json`, CLI / CI of the workspace) | start/stop runs and upload job sets for **every** project of the environment | read secrets, change IAM |
| Orchestration roles (migrate, post, job-runner, core and handoff Lambdas) | DMS describe; DMS start only for migrate on registered tasks; DynamoDB, run artifacts; StartExecution of post (handoff) or migrate (auto-resume) | read any database secret |
| Job role `<name>-<env>-job-<project>` (one per project, `job_roles.tf`) | that project's secrets (`connections[*].secret_arn`), its Data API clusters, its job sets (`jobs/<project>/`), run specs/results (`runs/`) | read another project's secrets or job sets |
| Shared `ecs-job` and `lambda-jobs` roles | DMS describe, run artifacts; `lambda-jobs` may assume project job roles and write `TARGET#` items (`dynamodb:LeadingKeys`, the restore baseline) | read secrets themselves; write any other DynamoDB item |

How a job gets its project role:

* Fargate: the job-runner passes `Overrides.TaskRoleArn = spec.jobRoleArn` to `ecs:RunTask`
  (it may `iam:PassRole` only the module's roles, only to ECS tasks);
* built-in Lambda jobs: `sts:AssumeRole` into `spec.jobRoleArn` for every Data API call;
* `prepare_job` takes `jobRoleArn` from the module's map by `ctx.project`; a project
  without a role fails the job set before anything runs.

The restore baseline (ADR 0006): `checks/target_verify` writes `TARGET#BASELINE` only in
the `restore` phase, which a job set cannot declare — `orch.py restore` starts it after a
successful restore. The record is always `source=restore` with a fingerprint the Lambda computed
itself, and `orch.py gate` never promotes on it: an operator who can restore can make the next
pre-flight accept the restored target, not fake a promotable run.

`job_secret_arns` stays the allow-list: every connection secret must be in it, and each
project role receives only the subset its own connections use. Job-set *code* is
uploaded by the operator; a project that needs its own upload boundary needs its own
operator role (or its own module instance).

