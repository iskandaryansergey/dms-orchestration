############################################
# IAM — one role per component, least privilege, policies via jsonencode (testable under mock_provider)
#
# Wildcard resources are used only where the service does not support resource-level
# permissions: dms:Describe* (DescribeReplicationTasks/TableStatistics/Connections/Events),
# ec2 ENI actions for Lambda in a VPC, CloudWatch Logs
# delivery actions for Step Functions, ecr:GetAuthorizationToken, ecs:DescribeTasks/StopTask
# (limited by the ecs:cluster condition), xray.
############################################

locals {
  # With create = false: "" for every function the policies name.
  function_arns = { for k in keys(local.lambda_functions_all) : k => local.create ? aws_lambda_function.this[k].arn : "" }

  kms_statements = local.use_cmk ? [{
    Sid      = "UseOrchestratorKey"
    Effect   = "Allow"
    Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    Resource = local.kms_key_arns
  }] : []

  xray_statements = var.enable_xray ? [{
    Sid      = "XRay"
    Effect   = "Allow"
    Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    Resource = "*"
  }] : []

  sfn_logs_statement = {
    Sid    = "VendedLogsDelivery"
    Effect = "Allow"
    Action = [
      "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
    ]
    Resource = "*"
  }

  lambda_vpc_statement = {
    Sid    = "LambdaInVpc"
    Effect = "Allow"
    Action = [
      "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface",
      "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses", "ec2:DescribeSubnets", "ec2:DescribeVpcs",
    ]
    Resource = "*"
  }

  table_arns = [local.table_arn, "${local.table_arn}/index/*"]

  dms_describe_statement = {
    Sid    = "DescribeDms"
    Effect = "Allow"
    Action = [
      "dms:DescribeReplicationTasks", "dms:DescribeTableStatistics", "dms:DescribeConnections",
      "dms:DescribeEvents", "dms:DescribeEndpoints",
    ]
    Resource = "*"
  }

  dms_log_statements = var.dms.log_group_name == "" ? [] : [{
    Sid      = "ReadDmsTaskLogs"
    Effect   = "Allow"
    Action   = ["logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams"]
    Resource = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:${var.dms.log_group_name}:*"]
  }]

  lambda_assume = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })

  states_assume = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:states:${local.region}:${local.account_id}:stateMachine:${local.prefix}-*" }
      }
    }]
  })

  ecs_assume = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:ecs:${local.region}:${local.account_id}:*" }
      }
    }]
  })

  ###### Policies

  # Shared by migrate and post: run items, job-runner sub-executions, notifications, logs.
  run_machine_statements = [
    {
      Sid      = "LockAndRunItems"
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:GetItem"]
      Resource = [local.table_arn]
    },
    {
      Sid      = "ManageJobRunnerExecutions"
      Effect   = "Allow"
      Action   = ["states:DescribeExecution", "states:StopExecution"]
      Resource = [local.job_runner_executions_arn]
    },
    {
      Sid      = "SyncIntegrationRule"
      Effect   = "Allow"
      Action   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
      Resource = ["arn:${local.partition}:events:${local.region}:${local.account_id}:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule"]
    },
    {
      Sid      = "Notify"
      Effect   = "Allow"
      Action   = ["sns:Publish"]
      Resource = [local.topic_arn]
    },
    local.sfn_logs_statement,
  ]

  # migrate: the only principal that starts DMS tasks (ADR 0005).
  migrate_policy = {
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "InvokeCoreLambdas"
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = flatten([for k in ["resolve_context", "inspect_task", "classify_failure", "write_report"] :
        [local.function_arns[k], "${local.function_arns[k]}:*"]])
      },
      {
        Sid      = "StartDmsTasks"
        Effect   = "Allow"
        Action   = ["dms:StartReplicationTask"]
        Resource = distinct(values(var.dms.task_arns))
      },
      {
        # RecheckTask: the task is read again right before the dump and the reset.
        Sid      = "RecheckDmsTasks"
        Effect   = "Allow"
        Action   = ["dms:DescribeReplicationTasks"]
        Resource = "*"
      },
      {
        # job-runner (.sync) and post (dry run hands over without DMS)
        Sid      = "StartJobRunnerAndPost"
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = [local.job_runner_arn, local.post_arn]
      },
      {
        Sid      = "SaveRunContext"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${local.bucket_arn}/runs/*"]
      },
      ], [for st in [
        {
          Sid      = "StartAssessments"
          Effect   = "Allow"
          Action   = ["dms:StartReplicationTaskAssessmentRun"]
          Resource = distinct(values(var.dms.task_arns))
        },
        {
          Sid      = "WatchAssessments"
          Effect   = "Allow"
          Action   = ["dms:DescribeReplicationTaskAssessmentRuns"]
          Resource = "*"
        },
        {
          Sid       = "PassAssessmentRole"
          Effect    = "Allow"
          Action    = ["iam:PassRole"]
          Resource  = [var.assessment.role_arn]
          Condition = { StringEquals = { "iam:PassedToService" = "dms.amazonaws.com" } }
        },
    ] : st if var.assessment.role_arn != ""], local.run_machine_statements, local.kms_statements, local.xray_statements)
  }

  # post: no DMS start; may start a new migrate for auto-resume.
  post_policy = {
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "InvokeCoreLambdas"
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = flatten([for k in ["resolve_context", "classify_failure", "write_report"] :
        [local.function_arns[k], "${local.function_arns[k]}:*"]])
      },
      {
        Sid      = "StartJobRunnerAndMigrate"
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = [local.job_runner_arn, local.migrate_arn]
      },
    ], local.run_machine_statements, local.kms_statements, local.xray_statements)
  }

  job_runner_policy = {
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "InvokeJobLambdas"
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = concat(
          [local.function_arns["prepare_job"], "${local.function_arns["prepare_job"]}:*"],
          [local.function_arns["builtin_jobs"], "${local.function_arns["builtin_jobs"]}:*"],
          var.lambda.sql_jobs_enabled ? [local.function_arns["sql_jobs"], "${local.function_arns["sql_jobs"]}:*"] : [],
          var.job_lambda_arns,
        )
      },
      {
        Sid      = "RecordJobs"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = [local.table_arn]
      },
      {
        Sid      = "ReadJobResults"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${local.bucket_arn}/runs/*"]
      },
      local.sfn_logs_statement,
      ], [for st in [
        {
          Sid       = "RunJobTasks"
          Effect    = "Allow"
          Action    = ["ecs:RunTask"]
          Resource  = [for f in values(local.task_definition_families) : "arn:${local.partition}:ecs:${local.region}:${local.account_id}:task-definition/${f}:*"]
          Condition = { ArnEquals = { "ecs:cluster" = local.cluster_arn } }
        },
        {
          Sid       = "TrackJobTasks"
          Effect    = "Allow"
          Action    = ["ecs:StopTask", "ecs:DescribeTasks"]
          Resource  = "*"
          Condition = { ArnEquals = { "ecs:cluster" = local.cluster_arn } }
        },
        {
          Sid       = "PassJobRoles"
          Effect    = "Allow"
          Action    = ["iam:PassRole"]
          Resource  = concat(aws_iam_role.ecs_execution[*].arn, aws_iam_role.ecs_task[*].arn, values(local.job_role_arns))
          Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
        },
        ] : st if local.fargate_enabled], [for st in [
        {
          # FindSsmRunner: no resource-level permissions.
          Sid      = "FindSsmRunners"
          Effect   = "Allow"
          Action   = ["ssm:DescribeInstanceInformation"]
          Resource = "*"
        },
        {
          Sid      = "SendJobDocument"
          Effect   = "Allow"
          Action   = ["ssm:SendCommand"]
          Resource = [local.ssm_document_arn]
        },
        {
          # GetSsmInvocation: no resource-level permissions.
          Sid      = "TrackJobCommands"
          Effect   = "Allow"
          Action   = ["ssm:GetCommandInvocation"]
          Resource = "*"
        },
        ] : st if local.ssm_enabled], local.ssm_enabled && length(var.ssm.instance_ids) > 0 ? [{
        Sid      = "SendToRunnerInstances"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = [for id in var.ssm.instance_ids : "arn:${local.partition}:ec2:${local.region}:${local.account_id}:instance/${id}"]
        }] : [], local.ssm_enabled && length(var.ssm.target_tags) > 0 ? [{
        # Only instances that carry every configured tag (the same tags FindSsmRunner filters on).
        Sid       = "SendToTaggedRunners"
        Effect    = "Allow"
        Action    = ["ssm:SendCommand"]
        Resource  = ["arn:${local.partition}:ec2:${local.region}:${local.account_id}:instance/*"]
        Condition = { StringEquals = { for k, v in var.ssm.target_tags : "ssm:resourceTag/${k}" => v } }
        }] : [], [{
        # Always: CreateStateMachine checks the managed rule of the ecs:runTask.sync
        # state, which is in the definition whether or not images are configured.
        Sid      = "SyncIntegrationRule"
        Effect   = "Allow"
        Action   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
        Resource = ["arn:${local.partition}:events:${local.region}:${local.account_id}:rule/StepFunctionsGetEventsForECSTaskRule"]
    }], local.kms_statements, local.xray_statements)
  }

  lambda_core_policy = {
    Version = "2012-10-17"
    Statement = concat([
      local.lambda_vpc_statement,
      local.dms_describe_statement,
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [for k, v in local.lambda_functions : "${aws_cloudwatch_log_group.lambda[k].arn}:*" if v.role == "core"]
      },
      {
        Sid      = "ReadTaskRegistrations"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = ["arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter${local.ssm_prefix}/*"]
      },
      {
        Sid      = "ReadJobSets"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${local.bucket_arn}/jobs/*"]
      },
      {
        Sid      = "WriteRunArtifacts"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = ["${local.bucket_arn}/runs/*"]
      },
      {
        # load_post reads context.json by the VersionId migrate recorded (RUN#.contextVersionId).
        Sid      = "ReadPinnedContext"
        Effect   = "Allow"
        Action   = ["s3:GetObjectVersion"]
        Resource = ["${local.bucket_arn}/runs/*/context.json"]
      },
      {
        # resolve_context, operator repeat of post: is the first post still running?
        Sid      = "CheckPostExecutions"
        Effect   = "Allow"
        Action   = ["states:DescribeExecution"]
        Resource = [local.post_executions_arn]
      },
      {
        # RUN#, TARGET# baseline and the TABLE# copy (write_report, ADR 0006)
        Sid      = "RunItems"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:BatchWriteItem", "dynamodb:DeleteItem"]
        Resource = local.table_arns
      },
    ], local.dms_log_statements, local.kms_statements, local.xray_statements)
  }

  lambda_waiter_policy = {
    Version = "2012-10-17"
    Statement = concat([
      local.lambda_vpc_statement,
      local.dms_describe_statement,
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [for k, v in local.lambda_functions : "${aws_cloudwatch_log_group.lambda[k].arn}:*" if v.role == "waiter"]
      },
      {
        # The handoff (ADR 0005): the event handler or the watchdog starts post once.
        Sid      = "StartPost"
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = [local.post_arn]
      },
      {
        Sid      = "CheckRunExecutions"
        Effect   = "Allow"
        Action   = ["states:DescribeExecution"]
        Resource = [local.migrate_executions_arn, local.post_executions_arn]
      },
      {
        Sid      = "MigrationAndLockItems"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query"]
        Resource = local.table_arns
      },
      {
        Sid      = "NotifyAbortedRuns"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = [local.topic_arn]
      },
      {
        Sid      = "DeadLetters"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [local.dlq_arn]
      },
    ], local.kms_statements, local.xray_statements)
  }

  lambda_jobs_policy = {
    Version = "2012-10-17"
    Statement = concat([
      local.lambda_vpc_statement,
      local.dms_describe_statement,
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [for k, v in local.lambda_functions : "${aws_cloudwatch_log_group.lambda[k].arn}:*" if v.role == "jobs"]
      },
      {
        # checks/target_verify after orch.py restore records the target baseline (ADR 0006);
        # TARGET# items only, nothing else in the table.
        Sid      = "RecordRestoreBaseline"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = [local.table_arn]
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["TARGET#*"] }
        }
      },
      {
        # Secrets and Data API only through the project's job role (job_roles.tf).
        Sid      = "AssumeProjectJobRoles"
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = values(local.job_role_arns)
      },
      ], length(var.dms.endpoint_arns) > 0 && var.dms.replication_instance_arn != null ? [{
        # checks/connections tests untested or failed endpoints before DMS starts.
        Sid      = "TestEndpointConnections"
        Effect   = "Allow"
        Action   = ["dms:TestConnection"]
        Resource = concat([var.dms.replication_instance_arn], values(var.dms.endpoint_arns))
    }] : [], local.kms_statements, local.xray_statements)
  }

  ecs_execution_policy = {
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid      = "PullImages"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = length(var.fargate.ecr_repository_arns) > 0 ? var.fargate.ecr_repository_arns : ["arn:${local.partition}:ecr:${local.region}:${local.account_id}:repository/*"]
      },
      {
        Sid      = "JobLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${local.jobs_log_group_arn}:*"]
      },
    ], local.kms_statements)
  }

  ecs_task_policy = {
    Version = "2012-10-17"
    Statement = concat([
      # Default task role only: the job-runner always overrides it with the project's job
      # role (Overrides.TaskRoleArn), which alone reaches runs/<project>/*/jobs/*.
      {
        Sid      = "ReadJobBundles"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${local.bucket_arn}/jobs/*"]
      },
      {
        Sid       = "ListJobBundles"
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = [local.bucket_arn]
        Condition = { StringLike = { "s3:prefix" = ["jobs/*"] } }
      },
    ], local.kms_statements)
  }

  scheduler_policy = {
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeWatchdog"
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = [local.function_arns["watchdog"], "${local.function_arns["watchdog"]}:*"]
    }]
  }

  # Not attached: for the caller's operator role (CLI / CI) in each environment. It runs
  # migrations; publishing job sets is jobset_publisher_policies (review finding 2).
  operator_policy = {
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "RunMigrations"
        Effect   = "Allow"
        Action   = ["states:StartExecution", "states:ListExecutions", "states:DescribeStateMachine"]
        Resource = [local.migrate_arn, local.post_arn]
      },
      {
        Sid      = "WatchMigrations"
        Effect   = "Allow"
        Action   = ["states:DescribeExecution", "states:StopExecution", "states:GetExecutionHistory"]
        Resource = [local.migrate_executions_arn, local.post_executions_arn, local.job_runner_executions_arn]
      },
      {
        # runs/<project>/<runId>/report.json|md (orch.py report)
        Sid      = "ReadReports"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${local.bucket_arn}/runs/*/report.json", "${local.bucket_arn}/runs/*/report.md"]
      },
      {
        # The wildcard above also matches a report.json a job writes among its artifacts
        # (runs/<project>/<runId>/jobs/...): what jobs write is never read back (review item N1).
        Sid      = "NoJobArtifacts"
        Effect   = "Deny"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = ["${local.bucket_arn}/runs/*/jobs/*"]
      },
      {
        # orch.py dumps: manifests only; the dumps themselves are read by the project job role
        Sid      = "ReadDumpManifests"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${local.bucket_arn}/dumps/*/manifest.json"]
      },
      {
        # orch.py restore runs pg-restore-s3 as a job-runner execution
        Sid      = "RestoreThroughJobRunner"
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = [local.job_runner_arn]
      },
      {
        Sid      = "ReadTaskRegistrations"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = ["arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter${local.ssm_prefix}/*"]
      },
      {
        Sid      = "ReadRunState"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query"]
        Resource = local.table_arns
      },
      {
        # orch.py restore holds the task lock (LOCK#<taskArn>) like a run; no other item.
        Sid       = "TaskLock"
        Effect    = "Allow"
        Action    = ["dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource  = [local.table_arn]
        Condition = { "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["LOCK#*"] } }
      },
      {
        Sid      = "StopDmsTasks"
        Effect   = "Allow"
        Action   = ["dms:StopReplicationTask"]
        Resource = distinct(values(var.dms.task_arns))
      },
      {
        Sid      = "DescribeDmsTasks"
        Effect   = "Allow"
        Action   = ["dms:DescribeReplicationTasks"]
        Resource = "*"
      },
    ], local.kms_statements)
  }
}

locals {
  # Not attached: one policy per project for whoever publishes that project's job sets
  # (scripts/sync-jobs.sh). Job set code runs with the project's job role, so uploading it
  # is a separate grant from running migrations (review finding 2).
  jobset_publisher_policies = {
    for p in keys(local.projects) : p => {
      Version = "2012-10-17"
      Statement = concat([
        {
          # Create only: PutObject must carry If-None-Match, so a published version is never
          # overwritten (the registration pins its bundle hash, review finding 9).
          Sid       = "PublishJobSets"
          Effect    = "Allow"
          Action    = ["s3:PutObject"]
          Resource  = [for prefix in local.project_job_prefixes[p] : "${local.bucket_arn}/${prefix}/*"]
          Condition = { Null = { "s3:if-none-match" = "false" } }
        },
        {
          Sid       = "ListJobSets"
          Effect    = "Allow"
          Action    = ["s3:ListBucket"]
          Resource  = [local.bucket_arn]
          Condition = { StringLike = { "s3:prefix" = [for prefix in local.project_job_prefixes[p] : "${prefix}/*"] } }
        },
      ], local.kms_statements)
    }
  }
}

resource "aws_iam_role" "migrate" {
  count                = local.create ? 1 : 0
  name                 = "${local.prefix}-sfn-migrate"
  assume_role_policy   = local.states_assume
  permissions_boundary = local.boundary
  tags                 = local.tags
}

resource "aws_iam_role_policy" "migrate" {
  count  = local.create ? 1 : 0
  name   = "migrate"
  role   = aws_iam_role.migrate[0].id
  policy = jsonencode(local.migrate_policy)
}

resource "aws_iam_role" "post" {
  count                = local.create ? 1 : 0
  name                 = "${local.prefix}-sfn-post"
  assume_role_policy   = local.states_assume
  permissions_boundary = local.boundary
  tags                 = local.tags
}

resource "aws_iam_role_policy" "post" {
  count  = local.create ? 1 : 0
  name   = "post"
  role   = aws_iam_role.post[0].id
  policy = jsonencode(local.post_policy)
}

resource "aws_iam_role" "job_runner" {
  count                = local.create ? 1 : 0
  name                 = "${local.prefix}-sfn-job-runner"
  assume_role_policy   = local.states_assume
  permissions_boundary = local.boundary
  tags                 = local.tags
}

resource "aws_iam_role_policy" "job_runner" {
  count  = local.create ? 1 : 0
  name   = "job-runner"
  role   = aws_iam_role.job_runner[0].id
  policy = jsonencode(local.job_runner_policy)
}

resource "aws_iam_role" "lambda_core" {
  count                = local.create ? 1 : 0
  name                 = "${local.prefix}-lambda-core"
  assume_role_policy   = local.lambda_assume
  permissions_boundary = local.boundary
  tags                 = local.tags
}

resource "aws_iam_role_policy" "lambda_core" {
  count  = local.create ? 1 : 0
  name   = "lambda-core"
  role   = aws_iam_role.lambda_core[0].id
  policy = jsonencode(local.lambda_core_policy)
}

resource "aws_iam_role" "lambda_waiter" {
  count                = local.create ? 1 : 0
  name                 = "${local.prefix}-lambda-waiter"
  assume_role_policy   = local.lambda_assume
  permissions_boundary = local.boundary
  tags                 = local.tags
}

resource "aws_iam_role_policy" "lambda_waiter" {
  count  = local.create ? 1 : 0
  name   = "lambda-waiter"
  role   = aws_iam_role.lambda_waiter[0].id
  policy = jsonencode(local.lambda_waiter_policy)
}

resource "aws_iam_role" "lambda_jobs" {
  count                = local.create ? 1 : 0
  name                 = "${local.prefix}-lambda-jobs"
  assume_role_policy   = local.lambda_assume
  permissions_boundary = local.boundary
  tags                 = local.tags
}

resource "aws_iam_role_policy" "lambda_jobs" {
  count  = local.create ? 1 : 0
  name   = "lambda-jobs"
  role   = aws_iam_role.lambda_jobs[0].id
  policy = jsonencode(local.lambda_jobs_policy)
}

resource "aws_iam_role" "ecs_execution" {
  count                = local.fargate_enabled ? 1 : 0
  name                 = "${local.prefix}-ecs-execution"
  assume_role_policy   = local.ecs_assume
  permissions_boundary = local.boundary
  tags                 = local.tags
}

resource "aws_iam_role_policy" "ecs_execution" {
  count  = local.fargate_enabled ? 1 : 0
  name   = "ecs-execution"
  role   = aws_iam_role.ecs_execution[0].id
  policy = jsonencode(local.ecs_execution_policy)
}

resource "aws_iam_role" "ecs_task" {
  count                = local.fargate_enabled ? 1 : 0
  name                 = "${local.prefix}-ecs-job"
  assume_role_policy   = local.ecs_assume
  permissions_boundary = local.boundary
  tags                 = local.tags
}

resource "aws_iam_role_policy" "ecs_task" {
  count  = local.fargate_enabled ? 1 : 0
  name   = "ecs-job"
  role   = aws_iam_role.ecs_task[0].id
  policy = jsonencode(local.ecs_task_policy)
}

resource "aws_iam_role" "scheduler" {
  count = local.create ? 1 : 0
  name  = "${local.prefix}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
  permissions_boundary = local.boundary
  tags                 = local.tags
}

resource "aws_iam_role_policy" "scheduler" {
  count  = local.create ? 1 : 0
  name   = "invoke-watchdog"
  role   = aws_iam_role.scheduler[0].id
  policy = jsonencode(local.scheduler_policy)
}
