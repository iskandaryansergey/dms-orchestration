data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  prefix     = "${var.name}-${var.environment}"
  region     = data.aws_region.current.name
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  tags = merge(var.tags, {
    "dms-orch:module"      = "dms-orchestration"
    "dms-orch:environment" = var.environment
  })

  use_cmk      = var.kms_key_arn != null
  kms_key_arns = local.use_cmk ? [var.kms_key_arn] : []

  # Storage and notifications: created or brought by the caller.
  create_table  = var.table_name == ""
  table_name    = local.create_table ? aws_dynamodb_table.this[0].name : var.table_name
  table_arn     = "arn:${local.partition}:dynamodb:${local.region}:${local.account_id}:table/${local.table_name}"
  create_bucket = var.artifacts_bucket_name == ""
  bucket_name   = local.create_bucket ? "${local.prefix}-artifacts-${local.account_id}" : var.artifacts_bucket_name
  bucket_arn    = "arn:${local.partition}:s3:::${local.bucket_name}"
  create_topic  = var.sns_topic_arn == ""
  topic_arn     = local.create_topic ? aws_sns_topic.this[0].arn : var.sns_topic_arn
  # Fargate is an optional third runtime (ADR 0011): ECS cluster, task definitions, ECS roles
  # and the ECR endpoints exist only when fargate.images is non-empty.
  fargate_enabled = length(var.fargate.images) > 0
  create_cluster  = local.fargate_enabled && var.fargate.cluster_arn == null
  cluster_arn     = !local.fargate_enabled ? null : local.create_cluster ? aws_ecs_cluster.this[0].arn : var.fargate.cluster_arn

  ssm_prefix = "/${var.name}/${var.environment}"

  # Two machines (ADR 0005): migrate (runs start here) and post (started by the DMS event).
  # ARNs are built from names so that each definition can name the other without a cycle.
  states_arn_prefix         = "arn:${local.partition}:states:${local.region}:${local.account_id}"
  migrate_name              = "${local.prefix}-migrate"
  post_name                 = "${local.prefix}-post"
  job_runner_name           = "${local.prefix}-job-runner"
  migrate_arn               = "${local.states_arn_prefix}:stateMachine:${local.migrate_name}"
  post_arn                  = "${local.states_arn_prefix}:stateMachine:${local.post_name}"
  job_runner_arn            = "${local.states_arn_prefix}:stateMachine:${local.job_runner_name}"
  migrate_executions_prefix = "${local.states_arn_prefix}:execution:${local.migrate_name}:"
  migrate_executions_arn    = "${local.migrate_executions_prefix}*"
  post_executions_arn       = "${local.states_arn_prefix}:execution:${local.post_name}:*"
  job_runner_executions_arn = "${local.states_arn_prefix}:execution:${local.job_runner_name}:*"

  # Every registered task: key "<project>/<taskKey>".
  tasks = merge([
    for project, p in var.projects : {
      for task_key, t in p.tasks : "${project}/${task_key}" => {
        project      = project
        task_key     = task_key
        task_arn_key = coalesce(t.task_arn_key, task_key)
        connections  = t.connections
        jobs         = t.jobs
        defaults     = t.defaults
      }
    }
  ]...)

  task_registrations = {
    for key, t in local.tasks : key => {
      schemaVersion          = "1"
      environment            = var.environment
      project                = t.project
      taskKey                = t.task_key
      taskArn                = var.dms.task_arns[t.task_arn_key]
      taskResourceId         = lookup(var.dms.task_resource_ids, t.task_arn_key, null)
      replicationInstanceArn = var.dms.replication_instance_arn
      replicationInstanceId  = var.dms.replication_instance_id
      logGroupName           = var.dms.log_group_name
      endpointArns           = values(var.dms.endpoint_arns)
      connections = {
        for name, c in t.connections : name => {
          engine            = c.engine
          secretArn         = c.secret_arn
          host              = c.host
          port              = c.port
          database          = c.database
          sslMode           = c.ssl_mode
          dataApiClusterArn = c.data_api_cluster_arn
          clusterArn        = c.cluster_arn
          auth              = c.auth
          iamUser           = c.iam_user
        }
      }
      jobs = {
        s3Prefix       = coalesce(t.jobs.s3_prefix, "jobs/${t.project}")
        defaultVersion = t.jobs.default_version
        bundles        = t.jobs.bundles
        allowUnpinned  = t.jobs.allow_unpinned
      }
      defaults = {
        completion             = t.defaults.completion
        targetResetPolicy      = t.defaults.target_reset_policy
        runTimeoutSeconds      = coalesce(t.defaults.run_timeout_seconds, var.run_timeout_seconds)
        autoResumeMaxAttempts  = t.defaults.auto_resume_max_attempts
        autoResumeCategories   = t.defaults.auto_resume_categories
        failOnTableErrors      = t.defaults.fail_on_table_errors
        failOnValidationErrors = t.defaults.fail_on_validation_errors
        dumpBeforeReset        = t.defaults.dump_before_reset
        snapshotDumps          = t.defaults.snapshot_dumps
        dumpRetentionDays      = var.dump_retention_days
      }
      # Command runtimes the module has (orch.py restore picks ssm when present).
      runtimes = {
        ssm     = var.ssm.enabled
        fargate = contains(keys(var.fargate.images), "sql-runner")
      }
      assessment = var.assessment == null ? null : {
        roleArn       = var.assessment.role_arn
        bucket        = var.assessment.bucket
        folder        = "${trimsuffix(var.assessment.folder, "/")}/${t.task_key}"
        kmsKeyArn     = var.assessment.kms_key_arn
        enabled       = var.assessment.enabled
        failOnWarning = var.assessment.fail_on_warning
      }
    }
  }

  # migrate and post never wait for DMS: a week for their jobs and retries. The per-task
  # runTimeoutSeconds is checked by the watchdog against MIGRATION# (MigrationOverdue).
  orchestration_timeout_seconds = 604800

  security_group_ids = concat(var.create_security_group ? [aws_security_group.jobs[0].id] : [], var.security_group_ids)

  task_definition_families = { for key, image in var.fargate.images : key => "${local.prefix}-${key}" }
}
