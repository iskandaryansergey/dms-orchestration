# Configuration reference: `modules/dms-orchestration`

The tables of inputs and outputs below the line are generated from `variables.tf` and
`outputs.tf` by [terraform-docs](https://terraform-docs.io) (`make docs`); CI fails when they
are out of date (`make docs-check`). Edit the descriptions in the `.tf` files, not here.
This hand-written part explains what a table cannot: the nested `projects` object, the
advisory checks and the outputs meant for other teams.

## `projects`: projects, tasks and their defaults

`projects` is a map of project name → `{ tasks = { <taskKey> = {...} } }`. The project name
and the task key (`taskKey`) are what a run request names (`project`, `taskKey`); they must
match `^[a-z][a-z0-9-]{0,39}$` and `^[a-z][a-z0-9_-]{0,39}$`. Each task is registered in SSM
for the state machines. Per task:

| Field | Default | Meaning |
|---|---|---|
| `tasks` | — | map of task key → task settings (the rows below) |
| `task_arn_key` | the task key | key in `dms.task_arns` of the replication task this task key drives; must exist there |
| `connections` | `{}` | map of connection name → database metadata for jobs and pre-flight; names `source` and `target` have a meaning (pre-flight, dumps) |
| `jobs` | `{}` | where the job set of the task lives in the artifacts bucket |
| `defaults` | `{}` | run defaults; a run request can override most of them |

### `connections[*]`

Metadata and secret ARNs only, never secret values. Every `secret_arn` must also be listed in
`job_secret_arns` (the allow-list for jobs).

| Field | Default | Meaning |
|---|---|---|
| `engine` | — (required) | database engine of the connection: `postgresql` (any engine name with `mysql` in it is treated as MySQL) |
| `secret_arn` | — (required with `auth = "secret"`) | Secrets Manager secret with the credentials; jobs and pre-flight read it at run time |
| `host` | from the secret | host name, when the secret does not carry it |
| `port` | from the secret | port, when the secret does not carry it |
| `database` | from the secret | database name |
| `ssl_mode` | `verify-full` | TLS mode for `psql`-based jobs; lower it only in a sandbox |
| `data_api_cluster_arn` | none | Aurora cluster ARN for RDS Data API jobs and the pre-flight target fingerprint |
| `cluster_arn` | `data_api_cluster_arn` | cluster to snapshot for `dump.method = snapshot` |
| `auth` | `secret` | how Fargate (`psql`) jobs log in: `secret` (credentials from `secret_arn`) or `iam` (a short-lived IAM database token, TLS only; DMS and the Data API keep using secrets) |
| `iam_user` | none | database user for `auth = "iam"`; it must have the `rds_iam` role in the engine |
| `iam_policy_arn` | none | policy with `rds-db:connect` for `iam_user` (the upstream aurora module outputs it per user); attached to the project's job role. `auth = "iam"` needs `engine = postgresql`, `host`, `iam_user` and `iam_policy_arn` |

### `jobs`

| Field | Default | Meaning |
|---|---|---|
| `s3_prefix` | `jobs/<project>` | prefix of the job set versions in the artifacts bucket |
| `default_version` | none | job set version used when the request names none; no version → the run has no jobs |

### `defaults`

| Field | Default | Meaning |
|---|---|---|
| `completion` | `auto` | when a run counts as loaded: `auto` (= `task-stopped` for full-load tasks) or `task-stopped` |
| `target_reset_policy` | `if-interrupted` | reset the target before a load: `never`, `if-interrupted` (after an interrupted load only) or `always` |
| `run_timeout_seconds` | module `run_timeout_seconds` | hard limit of one run; the watchdog stops runs that exceed it |
| `auto_resume_max_attempts` | `1` | how many times a run resumes by itself after a retryable failure |
| `auto_resume_categories` | `TRANSIENT_SOURCE_CONN`, `TRANSIENT_TARGET_CONN`, `DMS_RESTART` | failure categories that may resume by themselves |
| `fail_on_table_errors` | `true` | a load that finishes with tables in error fails the run instead of `COMPLETED_WITH_TABLE_ERRORS` |
| `fail_on_validation_errors` | `true` | DMS validation mismatches (`ValidationState` not `Validated`, failed records or full-load error rows) fail the run instead of `COMPLETED_WITH_VALIDATION_ERRORS` |
| `dump_before_reset` | `false` | take a target dump before every reset (as `dump.enabled` in the request) |
| `snapshot_dumps` | `false` | allow `dump.method = snapshot`: grants the job role cluster snapshot rights; needs `connections.target.cluster_arn` (or `data_api_cluster_arn`) |

Run-time semantics of these fields: [`RUNBOOK.md`](../RUNBOOK.md),
[`docs/state-machine.md`](state-machine.md), [`docs/preflight.md`](preflight.md),
[`docs/job-contract.md`](job-contract.md).

## `acknowledged_checks`: advisory checks

The module has `check` blocks: they warn at plan time and never fail an apply. List a check
name in `acknowledged_checks` to silence it once the trade-off is understood (typical in a
sandbox).

| Check | Warns when |
|---|---|
| `no_cmk` | `kms_key_arn` is not set: data at rest uses AWS managed keys |
| `include_execution_data` | `include_execution_data = true`: state input and output go to CloudWatch Logs |
| `fargate_image_tags` | a `fargate.images` entry has no tag or `:latest` (cannot be acknowledged; use an immutable tag) |
| `no_fargate_images` | `fargate.images` is empty: only Lambda jobs are possible |
| `lambda_reserved_concurrency` | `lambda.reserved_concurrency` is not set |
| `own_sns_topic_outside_sandbox` | `sns_topic_arn` is empty outside `environment = "sandbox"` |
| `state_in_the_dms_workspace` | `table_name` / `artifacts_bucket_name` are empty outside the sandbox: state would be destroyed with the DMS workspace |
| `dumps_without_cmk` | target dumps outside the sandbox use the AWS managed S3 key |

## Outputs for other teams

- `network_contract`: what the network must provide; the module creates no VPC endpoints.
  Required and optional interface endpoints, egress rules, the jobs security group to allow on
  the databases.
- `kms_key_policy_requirements`: JSON list of key policy statements a customer managed key
  needs when `kms_key_arn` is set; the module does not manage the key policy.
- `operator_policy_json`: IAM policy for the operator role (CLI or CI): start, watch and stop
  runs, read reports, take the task lock for `orch.py restore`. It cannot upload job sets. Not
  attached by the module.
- `jobset_publisher_policy_json`: map of project key to an IAM policy for whoever publishes that
  project's job sets (`scripts/sync-jobs.sh`): write and list `jobs/<project>/` only. Job set
  code runs with the project's job role, so this grant is separate from running migrations.

## Generated reference

<!-- BEGIN_TF_DOCS -->
### Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.9.0, < 2.0.0 |
| <a name="requirement_archive"></a> [archive](#requirement\_archive) | ~> 2.4 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | >= 5.90.0, < 6.0.0 |

### Providers

| Name | Version |
|------|---------|
| <a name="provider_archive"></a> [archive](#provider\_archive) | ~> 2.4 |
| <a name="provider_aws"></a> [aws](#provider\_aws) | >= 5.90.0, < 6.0.0 |

### Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_dms"></a> [dms](#input\_dms) | Outputs of the DMS module: replication instance, task ARNs and resource ids, endpoints, log group. | <pre>object({<br/>    replication_instance_arn = string<br/>    replication_instance_id  = string<br/>    task_arns                = map(string)<br/>    task_resource_ids        = optional(map(string), {})<br/>    endpoint_arns            = optional(map(string), {})<br/>    log_group_name           = optional(string)<br/>  })</pre> | n/a | yes |
| <a name="input_environment"></a> [environment](#input\_environment) | Environment name: sandbox, dev, stage, qa or prod. | `string` | n/a | yes |
| <a name="input_projects"></a> [projects](#input\_projects) | Projects and their DMS tasks. The map key of a task is the taskKey used in run requests.<br/>task\_arn\_key defaults to the task key and must exist in dms.task\_arns.<br/>connections hold metadata and secret ARNs only — never secret values. | <pre>map(object({<br/>    tasks = map(object({<br/>      task_arn_key = optional(string)<br/>      connections = optional(map(object({<br/>        engine               = string<br/>        secret_arn           = optional(string) # required with auth = "secret"<br/>        host                 = optional(string)<br/>        port                 = optional(number)<br/>        database             = optional(string)<br/>        ssl_mode             = optional(string, "verify-full")<br/>        data_api_cluster_arn = optional(string)<br/>        cluster_arn          = optional(string) # dump.method = snapshot; default data_api_cluster_arn<br/>        # auth = "iam": Fargate jobs log in as iam_user with an IAM token (psql only, TLS); the<br/>        # policy with rds-db:connect (upstream aurora module, iam_database_auth.users[u].policy_arn)<br/>        # is attached to the project's job role. DMS and the Data API keep using secrets.<br/>        auth           = optional(string, "secret")<br/>        iam_user       = optional(string)<br/>        iam_policy_arn = optional(string)<br/>      })), {})<br/>      jobs = optional(object({<br/>        s3_prefix       = optional(string)<br/>        default_version = optional(string)<br/>      }), {})<br/>      defaults = optional(object({<br/>        completion                = optional(string, "auto")<br/>        target_reset_policy       = optional(string, "if-interrupted")<br/>        run_timeout_seconds       = optional(number)<br/>        auto_resume_max_attempts  = optional(number, 1)<br/>        auto_resume_categories    = optional(list(string), ["TRANSIENT_SOURCE_CONN", "TRANSIENT_TARGET_CONN", "DMS_RESTART"])<br/>        fail_on_table_errors      = optional(bool, true)<br/>        fail_on_validation_errors = optional(bool, true) # tables DMS did not validate fail the run<br/>        dump_before_reset         = optional(bool, false)<br/>        snapshot_dumps            = optional(bool, false) # allow dump.method = snapshot (job role snapshot rights)<br/>      }), {})<br/>    }))<br/>  }))</pre> | n/a | yes |
| <a name="input_subnet_ids"></a> [subnet\_ids](#input\_subnet\_ids) | Private subnets (database or application tier) for Lambda and Fargate; at least two. | `list(string)` | n/a | yes |
| <a name="input_vpc_id"></a> [vpc\_id](#input\_vpc\_id) | VPC for Lambda functions and Fargate jobs. | `string` | n/a | yes |
| <a name="input_acknowledged_checks"></a> [acknowledged\_checks](#input\_acknowledged\_checks) | Advisory checks to silence, e.g. ["no\_cmk"] in a sandbox. | `list(string)` | `[]` | no |
| <a name="input_artifacts_bucket_name"></a> [artifacts\_bucket\_name](#input\_artifacts\_bucket\_name) | Existing bucket for job sets and run reports. Empty = create <name>-<environment>-artifacts-<account>. | `string` | `""` | no |
| <a name="input_artifacts_force_destroy"></a> [artifacts\_force\_destroy](#input\_artifacts\_force\_destroy) | Allow destroying the created artifacts bucket with objects in it (sandbox only). | `bool` | `false` | no |
| <a name="input_assessment"></a> [assessment](#input\_assessment) | DMS premigration assessment as a step of migrate, from the upstream<br/>module.dms.premigration\_assessment: role\_arn, bucket, folder (the task key is appended),<br/>kms\_key\_arn when the results use SSE-KMS. enabled = default for runs (a request may<br/>override it); fail\_on\_warning = a warning stops the run. null = no assessment step. | <pre>object({<br/>    role_arn        = string<br/>    bucket          = string<br/>    folder          = optional(string, "premigration-assessment")<br/>    kms_key_arn     = optional(string)<br/>    enabled         = optional(bool, false)<br/>    fail_on_warning = optional(bool, true)<br/>  })</pre> | `null` | no |
| <a name="input_container_insights"></a> [container\_insights](#input\_container\_insights) | Container Insights on the ECS cluster the module creates. Off by default: its log group outlives destroy. | `bool` | `false` | no |
| <a name="input_create_alarms"></a> [create\_alarms](#input\_create\_alarms) | Create CloudWatch alarms (state machine failures, Lambda errors, DLQ, watchdog not running). | `bool` | `true` | no |
| <a name="input_create_security_group"></a> [create\_security\_group](#input\_create\_security\_group) | Create the jobs security group (no ingress). When false, pass security\_group\_ids. | `bool` | `true` | no |
| <a name="input_database_cidr_blocks"></a> [database\_cidr\_blocks](#input\_database\_cidr\_blocks) | CIDR blocks of the database subnets for egress on database\_ports. | `list(string)` | `[]` | no |
| <a name="input_database_ports"></a> [database\_ports](#input\_database\_ports) | Database ports the jobs security group may reach. | `list(number)` | <pre>[<br/>  5432<br/>]</pre> | no |
| <a name="input_database_security_group_ids"></a> [database\_security\_group\_ids](#input\_database\_security\_group\_ids) | Security groups of the databases for egress on database\_ports (preferred over CIDRs). | `list(string)` | `[]` | no |
| <a name="input_dump_retention_days"></a> [dump\_retention\_days](#input\_dump\_retention\_days) | Days to keep target dumps: dumps/ in the created bucket (pg-dump-s3, S3 lifecycle) and cluster snapshots of dump.method = snapshot (deleted by the post job snapshot-retention). | `number` | `14` | no |
| <a name="input_enable_xray"></a> [enable\_xray](#input\_enable\_xray) | X-Ray tracing for state machines and Lambda. | `bool` | `false` | no |
| <a name="input_fargate"></a> [fargate](#input\_fargate) | Fargate jobs. images maps an image key (used as 'image' in job sets) to an image URI with an immutable tag. | <pre>object({<br/>    cluster_arn          = optional(string)<br/>    images               = optional(map(string), {})<br/>    cpu                  = optional(number, 1024)<br/>    memory               = optional(number, 2048)<br/>    ephemeral_storage_gb = optional(number, 21)<br/>    platform_version     = optional(string, "1.4.0")<br/>    assign_public_ip     = optional(bool, false)<br/>    ecr_repository_arns  = optional(list(string), [])<br/>  })</pre> | `{}` | no |
| <a name="input_https_egress_cidr_blocks"></a> [https\_egress\_cidr\_blocks](#input\_https\_egress\_cidr\_blocks) | IPv4 CIDRs the jobs security group may reach on 443 (interface VPC endpoints, a proxy). null = the VPC CIDR. 0.0.0.0/0 is refused: give a job that needs the internet an extra group via security\_group\_ids. | `list(string)` | `null` | no |
| <a name="input_https_egress_prefix_list_ids"></a> [https\_egress\_prefix\_list\_ids](#input\_https\_egress\_prefix\_list\_ids) | Prefix lists the jobs security group may reach on 443. null = the AWS managed S3 and DynamoDB prefix lists (gateway endpoints); [] = none. | `list(string)` | `null` | no |
| <a name="input_include_execution_data"></a> [include\_execution\_data](#input\_include\_execution\_data) | Log state input/output to CloudWatch Logs (ARNs, table names, job summaries, DMS messages; never secrets). Off by default; turn on for debugging (advisory check include\_execution\_data). | `bool` | `false` | no |
| <a name="input_job_lambda_arns"></a> [job\_lambda\_arns](#input\_job\_lambda\_arns) | Project-owned Lambda functions that job sets may call with 'function:'. | `list(string)` | `[]` | no |
| <a name="input_job_secret_arns"></a> [job\_secret\_arns](#input\_job\_secret\_arns) | Secrets Manager ARNs jobs may read (database users for pre/post jobs). Allow-list for IAM. | `list(string)` | `[]` | no |
| <a name="input_kms_key_arn"></a> [kms\_key\_arn](#input\_kms\_key\_arn) | Customer managed KMS key for DynamoDB, S3, SNS, SQS, logs, Lambda env and Step Functions. null = AWS managed keys (sandbox only; see check no\_cmk). Key policy requirements are in output kms\_key\_policy\_requirements. | `string` | `null` | no |
| <a name="input_lambda"></a> [lambda](#input\_lambda) | Lambda settings. artifact = prebuilt zip in S3 instead of packaging src/ with archive\_file. | <pre>object({<br/>    runtime              = optional(string, "python3.13")<br/>    architecture         = optional(string, "arm64")<br/>    memory_mb            = optional(number, 512)<br/>    reserved_concurrency = optional(number)<br/>    artifact_s3_bucket   = optional(string)<br/>    artifact_s3_key      = optional(string)<br/>    artifact_source_hash = optional(string)<br/>  })</pre> | `{}` | no |
| <a name="input_lock_grace_seconds"></a> [lock\_grace\_seconds](#input\_lock\_grace\_seconds) | Age after which the watchdog may remove a lock whose execution is no longer running. | `number` | `300` | no |
| <a name="input_log_retention_days"></a> [log\_retention\_days](#input\_log\_retention\_days) | Retention of all log groups created by the module. | `number` | `90` | no |
| <a name="input_name"></a> [name](#input\_name) | Base name for all resources (prefix is <name>-<environment>). | `string` | `"dms-orch"` | no |
| <a name="input_permissions_boundary_arn"></a> [permissions\_boundary\_arn](#input\_permissions\_boundary\_arn) | Permissions boundary for every IAM role created by the module. | `string` | `null` | no |
| <a name="input_report_retention_days"></a> [report\_retention\_days](#input\_report\_retention\_days) | Days to keep run reports under runs/ in the created bucket. | `number` | `365` | no |
| <a name="input_run_timeout_seconds"></a> [run\_timeout\_seconds](#input\_run\_timeout\_seconds) | Expected maximum duration of one DMS run; the watchdog raises MigrationOverdue after it (per-task override: projects.*.tasks.*.defaults.run\_timeout\_seconds). | `number` | `604800` | no |
| <a name="input_security_group_ids"></a> [security\_group\_ids](#input\_security\_group\_ids) | Extra (or, with create\_security\_group = false, the only) security groups for Lambda and Fargate. | `list(string)` | `[]` | no |
| <a name="input_sfn_log_level"></a> [sfn\_log\_level](#input\_sfn\_log\_level) | Step Functions log level: ALL, ERROR, FATAL or OFF. | `string` | `"ALL"` | no |
| <a name="input_sns_topic_arn"></a> [sns\_topic\_arn](#input\_sns\_topic\_arn) | Existing SNS topic for run notifications and alarms. Empty = create one. | `string` | `""` | no |
| <a name="input_table_deletion_protection"></a> [table\_deletion\_protection](#input\_table\_deletion\_protection) | Deletion protection on the DynamoDB table. | `bool` | `true` | no |
| <a name="input_table_name"></a> [table\_name](#input\_table\_name) | Existing DynamoDB table (same keys and indexes as this module creates). Empty = create <name>-<environment>. The target platform keeps it in a long-lived workspace so locks, runs and baselines outlive the DMS workspace. | `string` | `""` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Tags added to every resource. | `map(string)` | `{}` | no |
| <a name="input_watchdog_interval_minutes"></a> [watchdog\_interval\_minutes](#input\_watchdog\_interval\_minutes) | How often the watchdog checks armed migrations and locks. | `number` | `5` | no |

### Outputs

| Name | Description |
|------|-------------|
| <a name="output_artifacts_bucket"></a> [artifacts\_bucket](#output\_artifacts\_bucket) | Bucket for job sets (jobs/<project>/<version>/) and run artifacts (runs/<project>/<runId>/). |
| <a name="output_ecs_cluster_arn"></a> [ecs\_cluster\_arn](#output\_ecs\_cluster\_arn) | ECS cluster used for Fargate jobs. |
| <a name="output_jobs_security_group_id"></a> [jobs\_security\_group\_id](#output\_jobs\_security\_group\_id) | Security group of Lambda and Fargate jobs. Add it to the Aurora ingress rules (upstream aurora module: ingress\_rules.referenced\_security\_group\_id). |
| <a name="output_jobset_publisher_policy_json"></a> [jobset\_publisher\_policy\_json](#output\_jobset\_publisher\_policy\_json) | IAM policy per project for whoever publishes that project's job sets (scripts/sync-jobs.sh): write and list jobs/<project>/ only. Not attached by the module. |
| <a name="output_kms_key_policy_requirements"></a> [kms\_key\_policy\_requirements](#output\_kms\_key\_policy\_requirements) | JSON list of statements the CMK key policy must allow when kms\_key\_arn is set (the module does not manage the key policy). |
| <a name="output_lambda_function_arns"></a> [lambda\_function\_arns](#output\_lambda\_function\_arns) | Lambda functions of the module. |
| <a name="output_network_contract"></a> [network\_contract](#output\_network\_contract) | What the network must provide. The module creates no VPC endpoints (owned by the landing zone). |
| <a name="output_operator_policy_json"></a> [operator\_policy\_json](#output\_operator\_policy\_json) | IAM policy for the operator role (CLI / CI): start, watch and stop runs, read reports, restore under the task lock. No job set uploads (jobset\_publisher\_policy\_json). Not attached by the module. |
| <a name="output_run_request_example"></a> [run\_request\_example](#output\_run\_request\_example) | Example input for the migrate state machine. |
| <a name="output_sns_topic_arn"></a> [sns\_topic\_arn](#output\_sns\_topic\_arn) | Topic for run notifications and alarms. |
| <a name="output_ssm_parameter_names"></a> [ssm\_parameter\_names](#output\_ssm\_parameter\_names) | Task registrations per <project>/<taskKey>. |
| <a name="output_state_machine_arns"></a> [state\_machine\_arns](#output\_state\_machine\_arns) | Step Functions state machines: migrate (start runs here), post (started by the DMS event; operation=post repeats it) and job\_runner (internal). |
| <a name="output_table_arn"></a> [table\_arn](#output\_table\_arn) | ARN of the DynamoDB table (for an apply-time lock check in the DMS workspace). |
| <a name="output_table_name"></a> [table\_name](#output\_table\_name) | DynamoDB table with locks, runs, migrations, target baselines, events and job results. |
| <a name="output_task_definition_arns"></a> [task\_definition\_arns](#output\_task\_definition\_arns) | Fargate task definitions per image key. |
| <a name="output_verify_commands"></a> [verify\_commands](#output\_verify\_commands) | Commands to check the deployment without starting DMS. |
<!-- END_TF_DOCS -->
