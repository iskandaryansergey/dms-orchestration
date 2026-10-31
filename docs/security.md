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
  registered target clusters and with its project tag (`dms-orch:project`), and describes and
  deletes only snapshots with that tag. The boundary is the tag, not the snapshot identifier:
  tags may be added only to the project's own snapshots (the project tag cannot be changed to
  another project) or, on create, to a snapshot without the tag named like the project's, so a
  job cannot retag another project's snapshot and then delete it. No project name may be a
  prefix of another. That RDS evaluates `rds:req-tag` / `aws:RequestTag` on
  `CreateDBClusterSnapshot` as written is verified only by the sandbox e2e scenario `s10snap`;
  the Terraform tests check the policy text.
* Lambda and Fargate run in private subnets with a security group without ingress;
  databases allow it by security group reference. Its egress is 443 to the VPC CIDR (interface
  endpoints) and the S3/DynamoDB prefix lists (gateway endpoints) — never `0.0.0.0/0`
  (`https_egress_vpc_cidr` / `https_egress_gateway_endpoints` switch them off,
  `https_egress_cidr_blocks` / `https_egress_prefix_list_ids` add ranges; a job that needs the
  internet gets an extra group through `security_group_ids`) — plus `database_ports` to the databases.
* Optional customer managed KMS key for DynamoDB, S3, SNS, SQS, logs, Lambda environment,
  Step Functions and the scheduler. The module does not manage the key policy; it publishes what
  the policy must allow in two shapes built from one list: `kms_key_policy_requirements` (KMS key
  policy contract v2: per item the service principals, the module and job role ARNs, the
  symbolic roles `deployer` and `module`, actions, conditions such as `aws:SourceAccount`, and
  the purpose) and `kms_key_policy_statements` (the same as v1 key policy statements, concrete
  principals only). The service items are limited to this account by `aws:SourceAccount`; the
  deployer item by `kms:CallerAccount`.
  Without a CMK the SNS topic is unencrypted on purpose: the AWS managed key `alias/aws/sns`
  cannot allow CloudWatch alarms to publish, so alarms would be lost. With a CMK the key policy
  must allow `cloudwatch.amazonaws.com` (listed in both outputs).
* Fargate job containers run as uid 10001 with a read-only root file system and three
  scratch volumes (`/tmp`, `/work`, `/out`). Fargate mounts those empty and root-owned, so a
  non-essential init container of the same image runs as root for about a second to
  `chmod`/`chown` them and exits; it gets no task input, no secrets and no command override,
  and the job container starts only after it succeeded. A platform policy that forbids root
  containers outright would need a sidecar-free alternative (EFS access point with a fixed
  uid, or `VOLUME` directories owned by 10001 if the platform copies them) — open with the
  platform team.
* Isolated VPC: required endpoints are published in `network_contract`.
* Run artifacts are per project: `runs/<project>/<runId>/` (`context.json`, `report.*`, `jobs/`).
  A project's job role reads and writes only `runs/<project>/*/jobs/*`; the shared `ecs-job` and
  `lambda-jobs` roles have no access to `runs/`. migrate records the S3 version of the
  `context.json` it wrote on `RUN#` (`contextVersionId`) and post loads exactly that version under
  the project recorded on `RUN#`, otherwise it fails with `ContextNotPinned`. This needs a
  versioned artifacts bucket (the created bucket is; a caller-owned one must be): without a
  version the run stops before DMS.

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
| Cluster snapshots: create only of the project's target clusters with the project tag, tag only own or untagged ones (no retag to or from another project), describe and delete only tagged ones; no project name is a prefix of another (tag conditions on create proven by e2e `s10snap`) | `iam.tftest.hcl::snapshot_dumps_are_scoped_by_the_project_tag`, `validations.tftest.hcl::project_key_prefix_of_another_is_refused`, `test_jobs_lambdas.py::test_snapshot_retention_deletes_only_old_snapshots_of_this_task` |
| `rds-db:connect` only through the upstream aurora module's policy for the connection's user | `iam.tftest.hcl::iam_database_auth_for_fargate_jobs`, `validations.tftest.hcl::iam_connection_without_a_policy_is_refused` |
| The IAM token reaches only psql's environment, never a file, an argument or a log | `test_sql_runner.py::test_iam_token_reaches_psql_only_through_its_environment`, `test_sql_runner.py::test_iam_connection_file_holds_no_password_and_reads_no_secret`, `test_sql_runner.py::test_iam_connection_needs_tls` |
| Task tokens and secrets masked in logs | `test_shared_misc.py::test_log_masks_tokens` |
| Registration in SSM carries secret ARNs, never values | `defaults.tftest.hcl::storage_and_registration` |
| Jobs security group: no ingress; 443 egress only to the VPC CIDR and the gateway prefix lists | `defaults.tftest.hcl::lambda_and_events`, `validations.tftest.hcl::https_egress_to_the_internet`, `validations.tftest.hcl::https_egress_explicit_ranges` |
| Customer managed KMS key on every store and the key policy requirements (contract v2 and v1 statements) | `options.tftest.hcl::customer_managed_key`, `conventions.tftest.hcl::kms_contract_v2_shape` |
| Target dumps: SSE-KMS only, expire after `dump_retention_days` | `defaults.tftest.hcl::dumps_are_kms_only_and_expire` |
| Job roles reach only `runs/<project>/*/jobs/*`; `ecs-job` and `lambda-jobs` no run artifacts | `iam.tftest.hcl::per_project_job_roles`, `test_jobs_lambdas.py::test_builtin_jobs_write_artifacts_as_the_project_job_role` |
| post loads only the `context.json` version migrate recorded on `RUN#`, under the project on `RUN#` | `test_lambdas_core.py::test_post_loads_the_pinned_version_not_a_later_overwrite`, `test_lambdas_core.py::test_post_refuses_a_context_without_a_recorded_version`, `test_asl.py::test_migrate_without_a_context_version_stops_before_dms` |
| Operator: no job set uploads, reads only reports (never `runs/*/jobs/*`), writes only `LOCK#` items; job set publishing per project | `iam.tftest.hcl::operator_cannot_publish_job_sets` |
| job-runner input is not trusted: a job runs only if it is in the job set version recorded on the run (hash from the registration) with the same code, or is one of the module's own jobs with its fixed code (snapshot retention at least `dump_retention_days`), for a `RUNNING` run of the project named; the job context is built from the registration and `RUN#` only (reset list as inspect_task decided it); restore runs only `pg-restore-s3` of that task's dumps and the restore baseline check, under the task lock held by that restore execution; anything else is `JobNotInJobSet` | `test_job_trust.py::test_the_context_is_the_run_decision_not_the_input`, `test_job_trust.py::test_the_job_set_version_is_the_one_recorded_on_the_run`, `test_job_trust.py::test_snapshot_retention_keeps_at_least_the_registered_days`, `test_job_trust.py::test_restore_needs_the_task_lock_held_by_this_restore_execution`, `test_job_trust.py::test_an_inline_job_is_refused`, `test_job_trust.py::test_a_job_set_name_with_another_body_is_refused`, `test_job_trust.py::test_the_project_of_the_input_must_be_the_project_of_the_run`, `test_job_trust.py::test_connections_come_from_the_registration_not_the_input`, `test_job_trust.py::test_restore_accepts_nothing_else`, `test_job_trust.py::test_the_jobs_resolve_context_builds_are_accepted_as_they_are`, TestState `job-not-in-job-set-is-refused` |
| Restore baseline: written only in the `restore` phase, through `dynamodb:LeadingKeys` on `TARGET#` | `iam.tftest.hcl::least_privilege`, `test_jobs_lambdas.py::test_only_the_restore_phase_may_record_a_baseline` |
| Job container: uid 10001, read-only root, the init container gets no input or secrets | `defaults.tftest.hcl::lambda_and_events` |
| SSM runtime: `SendCommand` only of the module document to the configured runners; the runner role trusted by the job roles, never passed; instance profile reads `runtime/*` only; document parameters pattern-checked; runtime zip checked by SHA-256; jobs as `runner_user`, never root; off by default | `iam.tftest.hcl::ssm_runtime_least_privilege`, `iam.tftest.hcl::ssm_runtime_with_target_tags`, `options.tftest.hcl::ssm_runtime_with_a_platform_document`, `options.tftest.hcl::ssm_runtime_never_runs_jobs_as_root`, `defaults.tftest.hcl::no_ssm_runtime_by_default` |
| orch-job on SSM: the job role session is checked, no static keys reach the command, the connections and profile files are removed on every exit | `test_sql_runner.py::test_assume_role_session_refuses_another_identity`, `test_sql_runner.py::test_orch_job_on_ssm_runs_the_spec_command_with_the_job_role`, `test_sql_runner.py::test_connections_file_is_removed_when_the_job_wrapper_fails` |

## Trust model: who can read what

One module instance (one migration workspace per environment) serves many projects.
Projects trust the orchestrator, not each other.

| Principal | Trusted with | Cannot |
|---|---|---|
| Operator role (`operator_policy_json`, CLI / CI of the workspace) | start/stop runs and restores for **every** project of the environment, i.e. run the reviewed (pinned) jobs of every project and restore its target from its own dumps; read run-level reports; `LOCK#` items (`dynamodb:LeadingKeys`) | read secrets; run a job that is not in a pinned job set or the module's own; read job artifacts in S3 (`runs/*/jobs/*`, explicit Deny); upload job sets; change IAM. It does see job results in the execution history (below) |
| Job set publisher (`jobset_publisher_policy_json[<project>]`, one per project) | write and list that project's job set prefixes (`jobs/<project>/`) | another project's job sets; run artifacts; secrets |
| Orchestration roles (migrate, post, job-runner, core and handoff Lambdas) | DMS describe; DMS start only for migrate on registered tasks; DynamoDB, run artifacts (`runs/`); StartExecution of post (handoff) or migrate (auto-resume) | read any database secret |
| Job role `<name>-<env>-job-<project>` (one per project, `job_roles.tf`) | that project's secrets (`connections[*].secret_arn`), its Data API clusters, its job sets (`jobs/<project>/`), its runs' job specs, results and artifacts (`runs/<project>/*/jobs/*`) | read another project's secrets, job sets or run artifacts; write `context.json` or reports |
| Shared `ecs-job` and `lambda-jobs` roles | DMS describe; `ecs-job` (default task role, always overridden by the job role) reads job sets; `lambda-jobs` may assume project job roles and write `TARGET#` items (`dynamodb:LeadingKeys`, the restore baseline) | read secrets or run artifacts themselves; write any other DynamoDB item |
| SSM runner instance profile (`ssm.instance_role_arn`, caller-owned; `ssm_runner_policy_json`) | read the runtime zip (`runtime/*`); assume **every** project's job role (each job role trusts it); write command output to the jobs log group | read secrets, job sets or run artifacts itself; be passed to ECS or anything else (no `iam:PassRole` names it) |
| job-runner, SSM runtime | describe SSM instances; `ssm:SendCommand` only of the module's document (or `ssm.document_name_override`) and only to `ssm.instance_ids` or instances with every `ssm.target_tags` tag; read invocations | cancel commands, open sessions, change documents; send the command text (orch-job reads it from `spec.json`) |

How a job gets its project role:

* Fargate: the job-runner passes `Overrides.TaskRoleArn = spec.jobRoleArn` to `ecs:RunTask`
  (it may `iam:PassRole` only the module's roles, only to ECS tasks);
* built-in Lambda jobs: `sts:AssumeRole` into `spec.jobRoleArn` for every Data API call;
* SSM runner: the document passes `spec.jobRoleArn` (pattern-checked: only `<name>-<env>-job-*`
  of this account) and orch-job, as `ssm.runner_user`, assumes it from the instance profile and
  checks the session identity before it reads the spec; the document runs only a runtime zip
  whose SHA-256 Terraform recorded in it;
* `prepare_job` takes `jobRoleArn` from the module's map by the project of the run's `RUN#`
  item (restore: the registered task the input names); a project without a role fails the job
  set before anything runs.

Who may start the job-runner: migrate and post (nested executions) and the operator role, for
`orch.py restore`. Its input is therefore not trusted (review item N1). Before anything is
rendered or written, `prepare_job` (`shared/job_trust.py`):

* run phases (`preflight`, `baseline`, `pre`, `dump`, `reset`, `post`, `on_failure`): needs a
  `RUN#` item that is `RUNNING` — only the module's roles write `RUN#` — for the project and task
  the input names; looks the job up again by name in the job set version recorded on `RUN#`
  (`jobsVersion`; a repeat of post with a corrected version records it there), whose bundle hash
  it checks against the registration (`jobs.bundles`), and refuses a job whose code (`runtime`,
  `image`, `handler`, `function`, `command`, `sql`, `with`, `when`) differs; or accepts one of the
  module's own jobs (target check, dump, snapshot retention) only with exactly its code — the
  only free value is the target check's `exactCountMaxRows`, the snapshot retention is at least
  the registration's `dump_retention_days`. The job that runs is the one loaded, not the one
  passed;
* the job context is an allow-list, nothing of the input's `ctx` is kept: project, task, task ARN,
  environment, replication instance, endpoints and connections from the registration (a forged
  connection host never receives a password); job set version, attempt and the decision of the
  run — migration type, selection rules, start type, reset scope and reset list — from what
  inspect_task recorded on `RUN#`. A reset list kept in S3 is read only under
  `runs/<project>/<runId>/inspect/` of that run;
* `restore`: only `pg-restore-s3 --connection target --from <dump> --confirm` with a dump under
  `dumps/<project>/<task>/` of the artifacts bucket, and `checks/target_verify` recording the
  restore baseline for such a dump, scoped by the DMS task's table mapping; the run id must be a
  `restore-…` id, and `LOCK#<taskArn>` must be held by that restore id and by this very
  job-runner execution (as `orch.py restore` holds it), so a restore never runs under a run's
  lock;
* a run recorded as a dry run stays dry.

Anything else fails the job-runner with `JobNotInJobSet` (the execution's `JobFailed` cause).

The restore baseline (ADR 0006): `checks/target_verify` writes `TARGET#BASELINE` only in
the `restore` phase, which a job set cannot declare — `orch.py restore` starts it after a
successful restore. The record is always `source=restore` with a fingerprint the Lambda computed
itself, and `orch.py gate` never promotes on it: an operator who can restore can make the next
pre-flight accept the restored target, not fake a promotable run.

`job_secret_arns` stays the allow-list: every connection secret must be in it, and each
project role receives only the subset its own connections use. Job-set *code* is
uploaded by whoever holds that project's `jobset_publisher_policy_json`; the operator role
cannot upload it.

Residual risk, stated plainly:

* Job-set code runs with the project's job role and therefore can read that project's secrets.
  Whoever can publish a project's job sets can, in effect, read its secrets; attach the
  publisher policy only to principals trusted with them. A version runs only when its bundle
  hash is registered in Terraform (`jobs.bundles`; uploads are create-only), so what runs is
  what was reviewed and registered — unless `jobs.allow_unpinned` is on (sandbox only).
* The operator role still starts runs and restores for every project of the environment, and
  can restore a target from that task's dumps. While a run is `RUNNING` it can also start a
  job-runner execution that repeats one of that run's jobs (the same pinned job, the same
  recorded context) at a time it chooses; only the `failure` of an `on_failure` job is still
  taken from the input, and a job set that renders it into commands or SQL relies on the
  runtimes' quoting. A project that needs its own run boundary needs its own operator role per
  module instance.
* The restore lock check stops a restore started directly against the job-runner, not an operator
  who also rewrites the lock: the operator may write `LOCK#` items (it needs that for
  `orch.py restore`), so it could take a running migration's lock first. That breaks the
  migration's lock (the run then cannot release it as its own); it does not widen which jobs
  can run.
* What the operator reads: every project's run report (`runs/*/report.*`) from S3, never what
  jobs write under `runs/*/jobs/*`. It also reads the execution history of migrate, post and the
  job-runner (`GetExecutionHistory`): the routing output of `PrepareJob` (for Lambda jobs with the
  rendered `with` and the context — connection metadata, never secret values) and every job
  result (`status`, `message`, `summary`, including a summary row a sql job selects). Command
  output is not in results or execution causes (a failed job names its exit code and log stream
  only), but a job set that puts a secret into its `summary` hands it to the operator; it should
  not.
* SSM runtime: the runner instance profile may assume every project's job role, and jobs run
  under one local user (`ssm.runner_user`) that can reach the instance metadata service. A job of
  one project running on a runner can therefore act as another project's job role; anyone with
  an SSM session or `SendCommand` on a runner can do the same and, while a job runs, read its
  files and its connections file (0600, removed at the end). Projects sharing runners share
  this trust boundary. One module instance has one runner selection and one instance role, so
  this is not configurable per project today: a project that needs its own boundary needs its
  own module instance (and runners), and session access to the runners should be as narrow as
  access to the job roles.
* A project's job can overwrite its own run's job results and artifacts (`runs/<project>/*/jobs/*`),
  so results of one project's jobs are only as trustworthy as that project's job sets. It cannot
  reach `context.json` or reports, and post reads `context.json` by its pinned version.

