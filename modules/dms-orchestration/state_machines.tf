############################################
# Step Functions (Standard, JSONata) — ADR 0001, 0003
############################################

resource "aws_cloudwatch_log_group" "sfn" {
  count = local.create ? 1 : 0
  # /aws/vendedlogs/ prefix avoids the CloudWatch Logs resource policy size limit.
  name              = "/aws/vendedlogs/states/${local.prefix}"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.kms_key_arn
  tags              = local.tags
}

locals {
  job_runner_definition = !local.create ? null : jsonencode(jsondecode(templatefile("${path.module}/asl/job-runner.asl.json", {
    state_machine_timeout_seconds = 518400 # 6 days: longest job (24 h) x 5 attempts + margin
    prepare_job_arn               = aws_lambda_function.this["prepare_job"].arn
    table_name                    = local.table_name
    # Without fargate.images RunFargateJob is unreachable (prepare_job refuses runtime fargate,
    # no ecs:RunTask): the name the module would give its cluster keeps the definition valid.
    ecs_cluster_arn           = coalesce(local.cluster_arn, "arn:${local.partition}:ecs:${local.region}:${local.account_id}:cluster/${local.prefix}")
    fargate_platform_version  = var.fargate.platform_version
    subnets_json              = jsonencode(var.subnet_ids)
    security_groups_json      = jsonencode(local.security_group_ids)
    assign_public_ip          = var.fargate.assign_public_ip ? "ENABLED" : "DISABLED"
    artifacts_bucket          = local.bucket_name
    ssm_document_name         = local.ssm_document_name
    ssm_instance_filters_json = jsonencode(local.ssm_instance_filters)
    jobs_log_group_name       = aws_cloudwatch_log_group.jobs[0].name
  })))

  # Both run machines (ADR 0005) get the same values; each uses the ones it needs.
  run_machine_values = !local.create ? null : {
    state_machine_timeout_seconds = local.orchestration_timeout_seconds
    resolve_context_arn           = aws_lambda_function.this["resolve_context"].arn
    inspect_task_arn              = aws_lambda_function.this["inspect_task"].arn
    classify_failure_arn          = aws_lambda_function.this["classify_failure"].arn
    write_report_arn              = aws_lambda_function.this["write_report"].arn
    job_runner_arn                = aws_sfn_state_machine.job_runner[0].arn
    migrate_arn                   = local.migrate_arn
    migrate_executions_prefix     = local.migrate_executions_prefix
    post_arn                      = local.post_arn
    table_name                    = local.table_name
    topic_arn                     = local.topic_arn
    artifacts_bucket              = local.bucket_name
  }

  run_machines = !local.create ? {} : {
    migrate = {
      name       = local.migrate_name
      role_arn   = aws_iam_role.migrate[0].arn
      definition = jsonencode(jsondecode(templatefile("${path.module}/asl/migrate.asl.json", local.run_machine_values)))
    }
    post = {
      name       = local.post_name
      role_arn   = aws_iam_role.post[0].arn
      definition = jsonencode(jsondecode(templatefile("${path.module}/asl/post.asl.json", local.run_machine_values)))
    }
  }
}

resource "aws_sfn_state_machine" "job_runner" {
  count      = local.create ? 1 : 0
  name       = local.job_runner_name
  role_arn   = aws_iam_role.job_runner[0].arn
  type       = "STANDARD"
  definition = local.job_runner_definition
  # checkov:skip=CKV_AWS_284:X-Ray is opt-in (enable_xray); it needs the xray endpoint in isolated VPCs
  # checkov:skip=CKV_AWS_285:history is logged (sfn_log_level ALL); execution data stays out of logs by default (check include_execution_data)

  logging_configuration {
    level                  = var.sfn_log_level
    include_execution_data = var.include_execution_data
    log_destination        = "${aws_cloudwatch_log_group.sfn[0].arn}:*"
  }

  tracing_configuration {
    enabled = var.enable_xray
  }

  dynamic "encryption_configuration" {
    for_each = local.use_cmk ? [1] : []
    content {
      type                              = "CUSTOMER_MANAGED_KMS_KEY"
      kms_key_id                        = local.kms_key_arn
      kms_data_key_reuse_period_seconds = 300
    }
  }

  tags = local.tags

  # CreateStateMachine checks the role for the managed rule of ecs:runTask.sync.
  depends_on = [aws_iam_role_policy.job_runner]
}

resource "aws_sfn_state_machine" "run" {
  for_each   = local.run_machines
  name       = each.value.name
  role_arn   = each.value.role_arn
  type       = "STANDARD"
  definition = each.value.definition
  # checkov:skip=CKV_AWS_284:X-Ray is opt-in (enable_xray); it needs the xray endpoint in isolated VPCs
  # checkov:skip=CKV_AWS_285:history is logged (sfn_log_level ALL); execution data stays out of logs by default (check include_execution_data)

  logging_configuration {
    level                  = var.sfn_log_level
    include_execution_data = var.include_execution_data
    log_destination        = "${aws_cloudwatch_log_group.sfn[0].arn}:*"
  }

  tracing_configuration {
    enabled = var.enable_xray
  }

  dynamic "encryption_configuration" {
    for_each = local.use_cmk ? [1] : []
    content {
      type                              = "CUSTOMER_MANAGED_KMS_KEY"
      kms_key_id                        = local.kms_key_arn
      kms_data_key_reuse_period_seconds = 300
    }
  }

  tags = local.tags

  # CreateStateMachine checks the role (StartExecution.sync rule, Lambda invoke): policies first.
  depends_on = [aws_iam_role_policy.migrate, aws_iam_role_policy.post]
}

############################################
# ECS Fargate jobs (sql-runner image and others)
############################################

resource "aws_ecs_cluster" "this" {
  count = local.create_cluster ? 1 : 0
  name  = local.prefix

  setting {
    name  = "containerInsights"
    value = var.container_insights ? "enabled" : "disabled"
  }

  tags = local.tags
}

resource "aws_cloudwatch_log_group" "jobs" {
  count             = local.create ? 1 : 0
  name              = "/${var.name}/${var.environment}/jobs"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.kms_key_arn
  tags              = local.tags
}

resource "aws_ecs_task_definition" "job" {
  for_each                 = local.fargate_images
  family                   = local.task_definition_families[each.key]
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.fargate.cpu
  memory                   = var.fargate.memory
  execution_role_arn       = aws_iam_role.ecs_execution[0].arn
  task_role_arn            = aws_iam_role.ecs_task[0].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  ephemeral_storage {
    size_in_gib = var.fargate.ephemeral_storage_gb
  }

  # Read-only root file system; writable scratch volumes only.
  volume {
    name = "tmp"
  }
  volume {
    name = "work"
  }
  volume {
    name = "out"
  }

  # Fargate mounts these volumes empty, owned by root:root 0755, and does not copy the image
  # content into them (seen live, S1f): the non-root job container could not write.
  # A short init container of the same image, as root, opens them to uid 10001 and exits;
  # the job container starts only after it succeeded. No network, no secrets, no task input.
  container_definitions = jsonencode([{
    name                   = "init-volumes"
    image                  = each.value
    essential              = false
    readonlyRootFilesystem = true
    user                   = "0"
    entryPoint             = ["sh", "-c"]
    command                = ["chmod 1777 /tmp && chown 10001:10001 /work /out && chmod 0750 /work /out"]
    mountPoints = [
      { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false },
      { sourceVolume = "work", containerPath = "/work", readOnly = false },
      { sourceVolume = "out", containerPath = "/out", readOnly = false },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.jobs[0].name
        awslogs-region        = local.region
        awslogs-stream-prefix = "${each.key}-init"
      }
    }
    }, {
    name                   = "job"
    image                  = each.value
    essential              = true
    readonlyRootFilesystem = true
    user                   = "10001"
    dependsOn              = [{ containerName = "init-volumes", condition = "SUCCESS" }]
    environment = [
      { name = "ORCH_ARTIFACTS_BUCKET", value = local.bucket_name },
      { name = "ORCH_ENVIRONMENT", value = var.environment },
      { name = "ORCH_DUMP_KMS_KEY", value = var.kms_key_arn },
    ]
    mountPoints = [
      { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false },
      { sourceVolume = "work", containerPath = "/work", readOnly = false },
      { sourceVolume = "out", containerPath = "/out", readOnly = false },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.jobs[0].name
        awslogs-region        = local.region
        awslogs-stream-prefix = each.key
      }
    }
  }])

  tags = local.tags
}
