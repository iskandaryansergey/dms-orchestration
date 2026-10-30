############################################
# Naming
############################################

variable "name" {
  description = "Base name for all resources (prefix is <name>-<environment>)."
  type        = string
  default     = "dms-orch"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.name))
    error_message = "name must match ^[a-z][a-z0-9-]{1,20}$."
  }
}

variable "environment" {
  description = "Environment name: sandbox, dev, stage, qa or prod."
  type        = string

  validation {
    condition     = contains(["sandbox", "dev", "stage", "qa", "prod"], var.environment)
    error_message = "environment must be one of sandbox, dev, stage, qa, prod."
  }
}

variable "tags" {
  description = "Tags added to every resource."
  type        = map(string)
  default     = {}
}

############################################
# DMS — consumed from the upstream dms module outputs (nothing here is created again)
############################################

variable "dms" {
  description = "Outputs of the DMS module: replication instance, task ARNs and resource ids, endpoints, log group."
  type = object({
    replication_instance_arn = string
    replication_instance_id  = string
    task_arns                = map(string)
    task_resource_ids        = optional(map(string), {})
    endpoint_arns            = optional(map(string), {})
    log_group_name           = optional(string)
  })

  # The upstream dms module in compute_mode = "serverless" (or create = false) returns null
  # instance outputs and an empty task_arns: the orchestrator drives replication tasks
  # on an instance only (docs/upstream-modules-contract.md).
  validation {
    condition     = var.dms.replication_instance_arn != null && var.dms.replication_instance_id != null
    error_message = "dms.replication_instance_arn and dms.replication_instance_id must be set: the orchestrator supports the provisioned mode of the dms module only (serverless replication configs are not driven)."
  }

  validation {
    condition     = length(var.dms.task_arns) > 0
    error_message = "dms.task_arns must contain at least one replication task (empty in the dms module's serverless mode, which the orchestrator does not support)."
  }

  validation {
    condition     = alltrue([for arn in values(var.dms.task_arns) : can(regex("^arn:aws[a-z-]*:dms:[a-z0-9-]+:[0-9]{12}:task:[A-Za-z0-9-]+$", arn))])
    error_message = "every dms.task_arns value must be a DMS task ARN."
  }
}

############################################
# Projects and tasks (registered in SSM for the state machine)
############################################

variable "projects" {
  description = <<-EOT
    Projects and their DMS tasks. The map key of a task is the taskKey used in run requests.
    task_arn_key defaults to the task key and must exist in dms.task_arns.
    connections hold metadata and secret ARNs only — never secret values.
  EOT
  type = map(object({
    tasks = map(object({
      task_arn_key = optional(string)
      connections = optional(map(object({
        engine               = string
        secret_arn           = optional(string) # required with auth = "secret"
        host                 = optional(string)
        port                 = optional(number)
        database             = optional(string)
        ssl_mode             = optional(string, "verify-full")
        data_api_cluster_arn = optional(string)
        cluster_arn          = optional(string) # dump.method = snapshot; default data_api_cluster_arn
        # auth = "iam": Fargate jobs log in as iam_user with an IAM token (psql only, TLS); the
        # policy with rds-db:connect (upstream aurora module, iam_database_auth.users[u].policy_arn)
        # is attached to the project's job role. DMS and the Data API keep using secrets.
        auth           = optional(string, "secret")
        iam_user       = optional(string)
        iam_policy_arn = optional(string)
      })), {})
      jobs = optional(object({
        s3_prefix       = optional(string)
        default_version = optional(string)
      }), {})
      defaults = optional(object({
        completion                = optional(string, "auto")
        target_reset_policy       = optional(string, "if-interrupted")
        run_timeout_seconds       = optional(number)
        auto_resume_max_attempts  = optional(number, 1)
        auto_resume_categories    = optional(list(string), ["TRANSIENT_SOURCE_CONN", "TRANSIENT_TARGET_CONN", "DMS_RESTART"])
        fail_on_table_errors      = optional(bool, true)
        fail_on_validation_errors = optional(bool, true) # tables DMS did not validate fail the run
        dump_before_reset         = optional(bool, false)
        snapshot_dumps            = optional(bool, false) # allow dump.method = snapshot (job role snapshot rights)
      }), {})
    }))
  }))

  validation {
    condition     = alltrue([for p in keys(var.projects) : can(regex("^[a-z][a-z0-9-]{0,39}$", p))])
    error_message = "project names must match ^[a-z][a-z0-9-]{0,39}$ (same rule as the run request schema)."
  }

  validation {
    condition     = alltrue(flatten([for p in keys(var.projects) : [for q in keys(var.projects) : p == q || !startswith(q, p)]]))
    error_message = "no project name may be a prefix of another (for example ledger and ledger-prod): names built from a project name, such as snapshot identifiers, must not match another project's."
  }

  validation {
    condition     = alltrue(flatten([for p in values(var.projects) : [for k in keys(p.tasks) : can(regex("^[a-z][a-z0-9_-]{0,39}$", k))]]))
    error_message = "task keys must match ^[a-z][a-z0-9_-]{0,39}$."
  }

  validation {
    condition = alltrue(flatten([
      for p in values(var.projects) : [
        for k, t in p.tasks : contains(keys(var.dms.task_arns), coalesce(t.task_arn_key, k))
      ]
    ]))
    error_message = "every task (task_arn_key, or the task key itself) must exist in dms.task_arns."
  }

  validation {
    condition = alltrue(flatten([
      for p in values(var.projects) : [
        for t in values(p.tasks) : [
          contains(["auto", "task-stopped"], t.defaults.completion),
          contains(["never", "if-interrupted", "always"], t.defaults.target_reset_policy),
        ]
      ]
    ]))
    error_message = "defaults.completion must be auto|task-stopped (the first iteration is a full load) and defaults.target_reset_policy never|if-interrupted|always."
  }

  validation {
    condition = alltrue(flatten([
      for p in values(var.projects) : [
        for t in values(p.tasks) : !t.defaults.snapshot_dumps || try(coalesce(t.connections["target"].cluster_arn, t.connections["target"].data_api_cluster_arn), null) != null
      ]
    ]))
    error_message = "defaults.snapshot_dumps needs a connection named target with cluster_arn (or data_api_cluster_arn): the cluster to snapshot."
  }

  validation {
    condition = alltrue(flatten([
      for p in values(var.projects) : [
        for t in values(p.tasks) : [
          for c in values(t.connections) : c.secret_arn == null || contains(var.job_secret_arns, coalesce(c.secret_arn, "-"))
        ]
      ]
    ]))
    error_message = "every connections[*].secret_arn must be listed in job_secret_arns (the allow-list for jobs)."
  }

  validation {
    condition = alltrue(flatten([
      for p in values(var.projects) : [
        for t in values(p.tasks) : [
          for c in values(t.connections) : contains(["secret", "iam"], c.auth) && (c.auth == "iam" || c.secret_arn != null)
        ]
      ]
    ]))
    error_message = "connections[*].auth must be secret (with secret_arn) or iam."
  }

  validation {
    condition = alltrue(flatten([
      for p in values(var.projects) : [
        for t in values(p.tasks) : [
          for c in values(t.connections) : c.auth != "iam" || (c.iam_user != null && c.host != null && c.iam_policy_arn != null && c.engine == "postgresql")
        ]
      ]
    ]))
    error_message = "an auth = \"iam\" connection needs engine postgresql, iam_user, host and iam_policy_arn (the rds-db:connect policy of the upstream aurora module)."
  }
}

############################################
# Network
############################################

variable "vpc_id" {
  description = "VPC for Lambda functions and Fargate jobs."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets (database or application tier) for Lambda and Fargate; at least two."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "subnet_ids must contain at least two subnets."
  }
}

variable "create_security_group" {
  description = "Create the jobs security group (no ingress). When false, pass security_group_ids."
  type        = bool
  default     = true
}

variable "security_group_ids" {
  description = "Extra (or, with create_security_group = false, the only) security groups for Lambda and Fargate."
  type        = list(string)
  default     = []
}

variable "https_egress_cidr_blocks" {
  description = "IPv4 CIDRs the jobs security group may reach on 443 (interface VPC endpoints, a proxy). null = the VPC CIDR. 0.0.0.0/0 is refused: give a job that needs the internet an extra group via security_group_ids."
  type        = list(string)
  default     = null
  validation {
    condition     = var.https_egress_cidr_blocks == null ? true : alltrue([for c in var.https_egress_cidr_blocks : can(cidrhost(c, 0)) && !endswith(c, "/0")])
    error_message = "https_egress_cidr_blocks must be valid IPv4 CIDRs and must not be /0 (unrestricted egress)."
  }
}

variable "https_egress_prefix_list_ids" {
  description = "Prefix lists the jobs security group may reach on 443. null = the AWS managed S3 and DynamoDB prefix lists (gateway endpoints); [] = none."
  type        = list(string)
  default     = null
}

variable "database_ports" {
  description = "Database ports the jobs security group may reach."
  type        = list(number)
  default     = [5432]
}

variable "database_cidr_blocks" {
  description = "CIDR blocks of the database subnets for egress on database_ports."
  type        = list(string)
  default     = []
}

variable "database_security_group_ids" {
  description = "Security groups of the databases for egress on database_ports (preferred over CIDRs)."
  type        = list(string)
  default     = []
}

############################################
# Encryption, storage, notifications
############################################

variable "kms_key_arn" {
  description = "Customer managed KMS key for DynamoDB, S3, SNS, SQS, logs, Lambda env and Step Functions. null = AWS managed keys (sandbox only; see check no_cmk). Key policy requirements are in output kms_key_policy_requirements."
  type        = string
  default     = null

  validation {
    condition     = var.kms_key_arn == null ? true : can(regex("^arn:aws[a-z-]*:kms:[a-z0-9-]+:[0-9]{12}:key/[A-Za-z0-9-]+$", var.kms_key_arn))
    error_message = "kms_key_arn must be a KMS key ARN (arn:aws:kms:<region>:<account>:key/<id>)."
  }
}

variable "artifacts_bucket_name" {
  description = "Existing bucket for job sets and run reports. Empty = create <name>-<environment>-artifacts-<account>."
  type        = string
  default     = ""
}

variable "artifacts_force_destroy" {
  description = "Allow destroying the created artifacts bucket with objects in it (sandbox only)."
  type        = bool
  default     = false
}

variable "report_retention_days" {
  description = "Days to keep run reports under runs/ in the created bucket."
  type        = number
  default     = 365
}

variable "sns_topic_arn" {
  description = "Existing SNS topic for run notifications and alarms. Empty = create one."
  type        = string
  default     = ""
}

variable "table_deletion_protection" {
  description = "Deletion protection on the DynamoDB table."
  type        = bool
  default     = true
}

############################################
# Runtime tuning
############################################

variable "lambda" {
  description = "Lambda settings. artifact = prebuilt zip in S3 instead of packaging src/ with archive_file."
  type = object({
    runtime              = optional(string, "python3.13")
    architecture         = optional(string, "arm64")
    memory_mb            = optional(number, 512)
    reserved_concurrency = optional(number)
    artifact_s3_bucket   = optional(string)
    artifact_s3_key      = optional(string)
    artifact_source_hash = optional(string)
  })
  default = {}
}

variable "fargate" {
  description = "Fargate jobs. images maps an image key (used as 'image' in job sets) to an image URI with an immutable tag."
  type = object({
    cluster_arn          = optional(string)
    images               = optional(map(string), {})
    cpu                  = optional(number, 1024)
    memory               = optional(number, 2048)
    ephemeral_storage_gb = optional(number, 21)
    platform_version     = optional(string, "1.4.0")
    assign_public_ip     = optional(bool, false)
    ecr_repository_arns  = optional(list(string), [])
  })
  default = {}
}

variable "job_secret_arns" {
  description = "Secrets Manager ARNs jobs may read (database users for pre/post jobs). Allow-list for IAM."
  type        = list(string)
  default     = []
}

variable "job_lambda_arns" {
  description = "Project-owned Lambda functions that job sets may call with 'function:'."
  type        = list(string)
  default     = []
}

variable "watchdog_interval_minutes" {
  description = "How often the watchdog checks armed migrations and locks."
  type        = number
  default     = 5

  validation {
    condition     = var.watchdog_interval_minutes >= 1 && var.watchdog_interval_minutes <= 30
    error_message = "watchdog_interval_minutes must be between 1 and 30."
  }
}

variable "run_timeout_seconds" {
  description = "Expected maximum duration of one DMS run; the watchdog raises MigrationOverdue after it (per-task override: projects.*.tasks.*.defaults.run_timeout_seconds)."
  type        = number
  default     = 604800
}

variable "lock_grace_seconds" {
  description = "Age after which the watchdog may remove a lock whose execution is no longer running."
  type        = number
  default     = 300
}

############################################
# Observability
############################################

variable "log_retention_days" {
  description = "Retention of all log groups created by the module."
  type        = number
  default     = 90
}

variable "sfn_log_level" {
  description = "Step Functions log level: ALL, ERROR, FATAL or OFF."
  type        = string
  default     = "ALL"

  validation {
    condition     = contains(["ALL", "ERROR", "FATAL", "OFF"], var.sfn_log_level)
    error_message = "sfn_log_level must be ALL, ERROR, FATAL or OFF."
  }
}

variable "include_execution_data" {
  description = "Log state input/output to CloudWatch Logs (ARNs, table names, job summaries, DMS messages; never secrets). Off by default; turn on for debugging (advisory check include_execution_data)."
  type        = bool
  default     = false
}

variable "enable_xray" {
  description = "X-Ray tracing for state machines and Lambda."
  type        = bool
  default     = false
}

variable "create_alarms" {
  description = "Create CloudWatch alarms (state machine failures, Lambda errors, DLQ, watchdog not running)."
  type        = bool
  default     = true
}

############################################
# Governance
############################################

variable "permissions_boundary_arn" {
  description = "Permissions boundary for every IAM role created by the module."
  type        = string
  default     = null
}

variable "acknowledged_checks" {
  description = "Advisory checks to silence, e.g. [\"no_cmk\"] in a sandbox."
  type        = list(string)
  default     = []
}

variable "table_name" {
  description = "Existing DynamoDB table (same keys and indexes as this module creates). Empty = create <name>-<environment>. The target platform keeps it in a long-lived workspace so locks, runs and baselines outlive the DMS workspace."
  type        = string
  default     = ""
}

variable "container_insights" {
  description = "Container Insights on the ECS cluster the module creates. Off by default: its log group outlives destroy."
  type        = bool
  default     = false
}

variable "dump_retention_days" {
  description = "Days to keep target dumps: dumps/ in the created bucket (pg-dump-s3, S3 lifecycle) and cluster snapshots of dump.method = snapshot (deleted by the post job snapshot-retention)."
  type        = number
  default     = 14
}

variable "assessment" {
  description = <<-EOT
    DMS premigration assessment as a step of migrate, from the upstream
    module.dms.premigration_assessment: role_arn, bucket, folder (the task key is appended),
    kms_key_arn when the results use SSE-KMS. enabled = default for runs (a request may
    override it); fail_on_warning = a warning stops the run. null = no assessment step.
  EOT
  type = object({
    role_arn        = string
    bucket          = string
    folder          = optional(string, "premigration-assessment")
    kms_key_arn     = optional(string)
    enabled         = optional(bool, false)
    fail_on_warning = optional(bool, true)
  })
  default = null
}
