############################################
# Lambda code: one zip with src/lambdas and src/shared (pure Python). The only dependency is
# pg8000 for the Lambda SQL runtime, vendored with hashes in src/lambdas/sql_jobs/vendor
# together with the pinned RDS CA bundle: nothing is downloaded at build or run time.
############################################

data "archive_file" "lambda" {
  count       = var.lambda.artifact_s3_bucket == null ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/../../src"
  output_path = "${path.module}/.build/lambda-${local.prefix}.zip"
  excludes = [
    "jobs",
    "jobs/**",
    "**/__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
  ]
}

locals {
  lambda_env_common = {
    TABLE_NAME       = local.table_name
    ENVIRONMENT      = var.environment
    ARTIFACTS_BUCKET = local.bucket_name
    # Built-in jobs assume project job roles; in a VPC without internet only the
    # regional STS endpoint (VPC endpoint "sts") is reachable.
    AWS_STS_REGIONAL_ENDPOINTS = "regional"
  }

  sql_jobs_arn = "arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${local.prefix}-sql_jobs"

  # Lambda SQL runtime (ADR 0011): sql: jobs of job sets, pg8000 with TLS verify-full, the
  # project job role for secrets; the full 15 minutes and more memory than the other functions.
  sql_jobs_function = var.lambda.sql_jobs_enabled ? {
    sql_jobs = {
      description = "Lambda SQL runtime: sql jobs of job sets on PostgreSQL connections (pg8000, TLS verify-full)"
      timeout     = 900
      memory      = 1024
      role        = "jobs"
      env         = {}
    }
  } : {}

  # name => settings; role is one of core | waiter | jobs; memory defaults to lambda.memory_mb
  lambda_functions = merge(local.sql_jobs_function, {
    resolve_context = {
      description = "Validate run request, read task registration and job set"
      timeout     = 60
      role        = "core"
      # FARGATE_IMAGES: preflight.verifyRuntime auto/fargate needs the sql-runner image (verify-target).
      # SSM_JOBS: pg-dump dumps run on the SSM runtime when it is on.
      env = {
        SSM_PREFIX     = local.ssm_prefix
        FARGATE_IMAGES = jsonencode(sort(keys(var.fargate.images)))
        SSM_JOBS       = local.ssm_enabled ? "1" : "0"
      }
    }
    inspect_task = {
      description = "Classify DMS task state and decide start type / target reset"
      timeout     = 90
      role        = "core"
      env         = {}
    }
    classify_failure = {
      description = "Categorize failures, decide auto-resume, collect diagnostics"
      timeout     = 90
      role        = "core"
      env         = {}
    }
    write_report = {
      description = "Write run report to S3 and run status to DynamoDB"
      timeout     = 90
      role        = "core"
      env         = {}
    }
    prepare_job = {
      description = "Evaluate job conditions and templates, write job spec to S3"
      timeout     = 60
      role        = "core"
      env = {
        BUILTIN_JOBS_ARN = "arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${local.prefix}-builtin_jobs"
        SQL_JOBS_ARN     = var.lambda.sql_jobs_enabled ? local.sql_jobs_arn : ""
        TASK_DEFINITIONS = jsonencode({ for k, td in aws_ecs_task_definition.job : k => td.arn })
        JOB_LAMBDA_ARNS  = jsonencode(var.job_lambda_arns)
        JOB_ROLE_ARNS    = jsonencode(local.job_role_arns)
        SSM_JOBS         = local.ssm_enabled ? "1" : "0"
      }
    }
    dms_event_handler = {
      description = "DMS task state change -> describe task -> start post once"
      timeout     = 180 # re-describes for up to 120 s after a stop event
      role        = "waiter"
      env         = { POST_STATE_MACHINE_ARN = local.post_arn }
    }
    watchdog = {
      description = "Start post for lost DMS events, repeat stale claims, remove orphaned locks, flag overdue runs"
      timeout     = 240
      role        = "waiter"
      env = {
        LOCK_GRACE_SECONDS     = tostring(var.lock_grace_seconds)
        CLAIM_RETRY_SECONDS    = "120"
        POST_STATE_MACHINE_ARN = local.post_arn
      }
    }
    release_lock = {
      description = "Release the task lock when an execution fails, times out or is aborted"
      timeout     = 60
      role        = "waiter"
      env         = { TOPIC_ARN = local.topic_arn, POST_STATE_MACHINE_ARN = local.post_arn }
    }
    builtin_jobs = {
      description = "Built-in Lambda jobs: grants check, connection check, DMS diagnostics, control table cleanup"
      timeout     = 900
      role        = "jobs"
      env         = {}
    }
  })

  lambda_role_arns = {
    core   = aws_iam_role.lambda_core.arn
    waiter = aws_iam_role.lambda_waiter.arn
    jobs   = aws_iam_role.lambda_jobs.arn
  }

  lambda_reserved = toset(["dms_event_handler", "watchdog"])
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = local.lambda_functions
  name              = "/aws/lambda/${local.prefix}-${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = local.tags
}

resource "aws_lambda_function" "this" {
  for_each      = local.lambda_functions
  function_name = "${local.prefix}-${each.key}"
  description   = each.value.description
  role          = local.lambda_role_arns[each.value.role]
  runtime       = var.lambda.runtime
  architectures = [var.lambda.architecture]
  handler       = "lambdas.${each.key}.handler.handler"
  memory_size   = try(each.value.memory, var.lambda.memory_mb)
  timeout       = each.value.timeout

  filename         = var.lambda.artifact_s3_bucket == null ? data.archive_file.lambda[0].output_path : null
  source_code_hash = var.lambda.artifact_s3_bucket == null ? data.archive_file.lambda[0].output_base64sha256 : var.lambda.artifact_source_hash
  s3_bucket        = var.lambda.artifact_s3_bucket
  s3_key           = var.lambda.artifact_s3_key

  reserved_concurrent_executions = contains(local.lambda_reserved, each.key) && var.lambda.reserved_concurrency != null ? var.lambda.reserved_concurrency : -1
  kms_key_arn                    = var.kms_key_arn

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = local.security_group_ids
  }

  environment {
    variables = merge(local.lambda_env_common, each.value.env)
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda[each.key].name
  }

  tracing_config {
    mode = var.enable_xray ? "Active" : "PassThrough"
  }

  tags = local.tags

  depends_on = [aws_cloudwatch_log_group.lambda]
}

# DMS events -> handler is asynchronous; failed invocations go to the DLQ.
resource "aws_lambda_function_event_invoke_config" "dms_event_handler" {
  function_name                = aws_lambda_function.this["dms_event_handler"].function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 3600
  destination_config {
    on_failure {
      destination = aws_sqs_queue.dlq.arn
    }
  }
}

resource "aws_lambda_function_event_invoke_config" "release_lock" {
  function_name          = aws_lambda_function.this["release_lock"].function_name
  maximum_retry_attempts = 2
  destination_config {
    on_failure {
      destination = aws_sqs_queue.dlq.arn
    }
  }
}

resource "aws_lambda_permission" "dms_events" {
  statement_id  = "AllowDmsEventRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this["dms_event_handler"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.dms_task_state.arn
}

resource "aws_lambda_permission" "sfn_status" {
  statement_id  = "AllowExecutionStatusRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this["release_lock"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.execution_status.arn
}

############################################
# Security group for Lambda and Fargate jobs: no ingress
############################################

resource "aws_security_group" "jobs" {
  # checkov:skip=CKV2_AWS_5:attached to the Lambdas and Fargate tasks through local.security_group_ids (concat), which checkov does not follow
  count       = var.create_security_group ? 1 : 0
  name        = "${local.prefix}-jobs"
  description = "DMS orchestration Lambda and Fargate jobs (no ingress)"
  vpc_id      = var.vpc_id
  tags        = merge(local.tags, { Name = "${local.prefix}-jobs" })
}

# 443 only to where the AWS APIs live in an endpoint-only VPC (docs/architecture.md,
# network_contract): interface endpoints inside the VPC CIDR and the S3/DynamoDB
# gateway endpoints (AWS managed prefix lists; ECR image layers come from S3).
# Never 0.0.0.0/0: a job that really needs the internet gets an extra security group.
data "aws_vpc" "jobs" {
  count = var.create_security_group && var.https_egress_cidr_blocks == null ? 1 : 0
  id    = var.vpc_id
}

data "aws_prefix_list" "gateway" {
  for_each = var.create_security_group && var.https_egress_prefix_list_ids == null ? toset(["s3", "dynamodb"]) : toset([])
  name     = "com.amazonaws.${data.aws_region.current.name}.${each.key}"
}

resource "aws_vpc_security_group_egress_rule" "https_cidr" {
  for_each          = var.create_security_group ? toset(var.https_egress_cidr_blocks != null ? var.https_egress_cidr_blocks : data.aws_vpc.jobs[*].cidr_block) : toset([])
  security_group_id = aws_security_group.jobs[0].id
  description       = "AWS APIs via interface VPC endpoints"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = each.value
}

resource "aws_vpc_security_group_egress_rule" "https_prefix_list" {
  for_each = var.create_security_group ? {
    for i, id in var.https_egress_prefix_list_ids != null ? var.https_egress_prefix_list_ids : [for k in ["s3", "dynamodb"] : data.aws_prefix_list.gateway[k].id] : tostring(i) => id
  } : {}
  security_group_id = aws_security_group.jobs[0].id
  description       = "AWS APIs via gateway VPC endpoints"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = each.value
}

resource "aws_vpc_security_group_egress_rule" "db_cidr" {
  for_each          = var.create_security_group ? { for pair in setproduct(var.database_ports, var.database_cidr_blocks) : "${pair[0]}-${pair[1]}" => pair } : {}
  security_group_id = aws_security_group.jobs[0].id
  description       = "Databases"
  ip_protocol       = "tcp"
  from_port         = each.value[0]
  to_port           = each.value[0]
  cidr_ipv4         = each.value[1]
}

# Keyed by position, not by id: the database security groups are often created in
# the same plan (the upstream aurora module), and unknown ids cannot be for_each keys.
resource "aws_vpc_security_group_egress_rule" "db_sg" {
  for_each = var.create_security_group ? {
    for pair in setproduct(var.database_ports, range(length(var.database_security_group_ids))) :
    "${pair[0]}-${pair[1]}" => { port = pair[0], sg = var.database_security_group_ids[pair[1]] }
  } : {}
  security_group_id            = aws_security_group.jobs[0].id
  description                  = "Databases"
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  referenced_security_group_id = each.value.sg
}
