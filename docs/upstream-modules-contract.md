# Contract with the upstream `aurora` and `dms` modules

The orchestrator does not create the database clusters or the DMS resources. A
deployment takes them from two upstream Terraform modules: an **upstream `dms`
module** (replication instance, endpoints, tasks, log group) and an **upstream
`aurora` module** (the source and target clusters). Any implementation that satisfies
this contract fits; the name of the implementation is not part of it.

`modules/dms-orchestration` consumes only `variable "dms"`; the sandbox
(`examples/sandbox`) wires one implementation of both modules, pinned to a commit.

## 1. `dms` module outputs → our `dms = {...}` input

The names are the same, so the adapter is a plain field-by-field copy:

```hcl
module "dms_orchestration" {
  # ...
  dms = {
    replication_instance_arn = module.dms.replication_instance_arn
    replication_instance_id  = module.dms.replication_instance_id
    task_arns                = module.dms.task_arns
    task_resource_ids        = module.dms.task_resource_ids
    endpoint_arns            = module.dms.endpoint_arns
    log_group_name           = module.dms.log_group_name
  }
}
```

| Our field | Type (ours) | Upstream output | Type / value there | Match |
|---|---|---|---|---|
| `replication_instance_arn` | `string`, required | `replication_instance_arn` | `string`; **`null` in serverless mode** | ✅ provisioned; serverless refused (see 2.1) |
| `replication_instance_id` | `string`, required | `replication_instance_id` | `string` = `var.name`; `null` in serverless | ✅ same |
| `task_arns` | `map(string)`, ≥ 1 | `task_arns` | `map(string)` keyed by task name; `{}` in serverless | ✅ keys are the `tasks` keys → our `task_arn_key` |
| `task_resource_ids` | `map(string)`, optional | `task_resource_ids` | last ARN segment per task | ✅ (missing in older versions — optional, so still fine) |
| `endpoint_arns` | `map(string)`, optional | `endpoint_arns` | per endpoint key, **includes S3 targets** | ✅ we only use the values for DescribeConnections |
| `log_group_name` | `string`, optional | `log_group_name` | `dms-tasks-<name>`; `null` in serverless or `create = false` | ✅ |

Outputs an implementation may have that we do not take (and why):

| Upstream output | Why not |
|---|---|
| `task_ids` | task names; we key by ARN |
| `validation_task_arns` | validation-only tasks are not driven by the orchestrator; run them separately |
| `serverless_replication_arns` | serverless replication configs are not supported (2.1) |
| `endpoint_secret_role_arns`, `s3_target_role_arns`, `certificate_arns` | DMS-internal |
| `replication_instance_private_ips`, `network_contract` | firewall requests, not runtime |
| `task_settings_json`, `governance`, `verify_commands`, `*_requirements`, `premigration_assessment` | review/ops data |

Tests: `modules/dms-orchestration/tests/upstream_contract.tftest.hcl` feeds the exact
output shapes (with every extra output present, and without `task_resource_ids` as in
older versions) and the serverless shape.

## 2. Behaviour the orchestrator relies on

### 2.1 Serverless mode (`compute_mode = "serverless"`) — not supported, refused at plan

In serverless mode the `dms` module returns `replication_instance_arn = null`,
`task_arns = {}` and the work in `serverless_replication_arns`
(`aws_dms_replication_config`). The orchestrator drives `StartReplicationTask` on
replication tasks only. A `null` instance ARN would otherwise pass validation and reach
the SSM registration as `null`, so the validation on `dms` refuses it with a message
that names the serverless mode; tests `upstream_serverless_mode_refused`,
`null_replication_instance_refused`.
**Not in the MVP.** Serverless DMS has another API
(`StartReplication`/`StopReplication` on a replication config, its own states and
events, capacity in DCU, table statistics through `DescribeReplicationTableStatistics`):
a second DMS adapter in inspect/start/wait/classify, with its own tests. The provisioned
mode is the only one with per-task control. Serverless support is a later extension, if
the target platform's workspaces move to it.

### 2.2 Pass the outputs one by one, not `dms = module.dms`

Passing the whole module object type-checks (extra attributes are dropped), but makes
our module depend on every output of the upstream module, and some of them may depend on
its `check` blocks: `terraform plan` then fails with `Error: Cycle: (execute checks) ...
module.dms_orchestration.aws_ssm_parameter.task`. The adapter above avoids it.

### 2.3 Known defect of one implementation: endpoint secrets created in the same plan → `Invalid for_each argument`

One implementation filters endpoints on `e.secret_arn != ""` to build the `for_each` of
the per-endpoint secret-access IAM role. When the secret is created in the same
configuration its ARN is unknown at plan, so the map keys are unknown and the plan
fails. Workaround in the sandbox: `terraform apply -target=aws_secretsmanager_secret.db`
first. The same unknown keys fail a second `destroy` once the secrets are gone;
workaround: `destroy -target` by `state list` (sandbox README).
Fix for such an implementation: key the map on static configuration —
`e.username == "" && e.secret_access_role_arn == ""` (an endpoint is on the secret path
exactly when it has no username; a validation requires one credential source).

### 2.4 Defaults the orchestrator relies on

| Upstream `dms` module default | Effect on the orchestrator | Status |
|---|---|---|
| `tasks[*].start = false` | task created stopped; the orchestrator starts it | ✅ required |
| any `terraform apply` that changes a running task **stops it** | a run in progress sees an unexpected stop | RUNBOOK: no applies of the dms module during a run |
| `target_table_prep_mode = DO_NOTHING`; `DROP_AND_CREATE`/`TRUNCATE_BEFORE_LOAD` need an explicit opt-in | the target schema must exist; truncation on reset is our `reset` job | ✅ consistent with `target_reset_policy` |
| `control_schema = "dms_control"` | the schema must exist on the target before the first start | sandbox seed creates it; on the target platform — the target-schema pipeline |
| `validation_enabled = true` | full-load task keeps running for validation after the load | handled by the outcome evaluation; validation mismatches fail the run unless `fail_on_validation_errors = false` |
| `TableErrorPolicy = SUSPEND_TABLE`, escalation `STOP_TASK` after `table_error_max` (50) | a table error does not fail the task | our `fail_on_table_errors` defaults to `true`: the run fails `DmsTableErrors` before post-jobs |
| `create_log_group = true`, `log_retention_days = 30`, **`log_group_skip_destroy = true`** | our log tail works from the first run; the group survives `destroy` | sandbox sets `log_group_skip_destroy = false` |
| `create_event_subscriptions` (SNS) | independent of our EventBridge rule on `aws.dms` | ✅ no conflict |
| `create_account_service_roles = false` | `dms-vpc-role`, `dms-cloudwatch-logs-role` must exist | sandbox: `true` (account has none) |
| no TestConnection in the module | a task does not start until each endpoint passed TestConnection | the orchestrator tests them in `builtin:checks/connections` (IAM `dms:TestConnection` on the registered endpoints) |
| endpoints: `secret_arn` path, secret holds host/port/username/password; RDS-managed master secret not usable | — | sandbox uses own secrets |

### 2.5 `aurora` module

| What we need | Upstream `aurora` module | Note |
|---|---|---|
| Data API for the SQL built-ins | `enable_data_api` (may default to **false**) | must be `true` where built-in SQL jobs are used |
| `data_api_cluster_arn` on a connection | output `cluster_arn` | ✅ |
| `host`, `port`, `database` on a connection | output `connection.host/port/database` or `cluster_endpoint` | ✅ |
| secret for jobs | `connection.secret_arn` = RDS-managed master secret (username/password only) | Data API accepts it; jobs that need `host` (pg8000/Fargate) need their own secret |
| IAM database authentication for Fargate jobs | `create_iam_database_users`, output `iam_database_auth.users[u].policy_arn` | only for `connections[*].auth = "iam"` |
| Serverless v2, auto-pause | `instance_class = "db.serverless"`, `serverlessv2_scaling_configuration.min_capacity = 0`, `seconds_until_auto_pause` ≥ 300 | builtin jobs retry `DatabaseResumingException` |
| our jobs/DMS SGs allowed in | `ingress_rules = { name = { referenced_security_group_id = ... } }` | ✅; SG id via output `security_group_id` → our `database_security_group_ids` |

### 2.6 Known defect of one implementation: `MaxFileSize = 0` on a PostgreSQL target

One implementation renders `postgres_settings` for a PostgreSQL target without
`max_file_size`; provider 5.100 then sends 0. DMS 3.5.4 never loads a row (sandbox:
every table `Before load` for 8 and 40 min; with 32768 KB the load takes ~10 s).
Sandbox workaround: `sbx test-connections` sets 32768 through `ModifyEndpoint`.
Fix for such an implementation: `postgres.max_file_size = optional(number, 32768)`, sent
for targets and part of the target's settings hash (left out of the source hash, so
sources are not replaced). Existing PostgreSQL target endpoints are replaced once on
upgrade (the provider sends `postgres_settings` on create only), and their tasks with
them.
