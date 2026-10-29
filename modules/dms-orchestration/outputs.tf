output "state_machine_arns" {
  description = "Step Functions state machines: migrate (start runs here), post (started by the DMS event; operation=post repeats it) and job_runner (internal)."
  value = {
    migrate    = aws_sfn_state_machine.run["migrate"].arn
    post       = aws_sfn_state_machine.run["post"].arn
    job_runner = aws_sfn_state_machine.job_runner.arn
  }
}

output "table_name" {
  description = "DynamoDB table with locks, runs, migrations, target baselines, events and job results."
  value       = local.table_name
}

output "table_arn" {
  description = "ARN of the DynamoDB table (for an apply-time lock check in the DMS workspace)."
  value       = local.table_arn
}

output "artifacts_bucket" {
  description = "Bucket for job sets (jobs/<project>/<version>/) and run reports (runs/<runId>/)."
  value       = local.bucket_name
}

output "sns_topic_arn" {
  description = "Topic for run notifications and alarms."
  value       = local.topic_arn
}

output "jobs_security_group_id" {
  description = "Security group of Lambda and Fargate jobs. Add it to the Aurora ingress rules (upstream aurora module: ingress_rules.referenced_security_group_id)."
  value       = var.create_security_group ? aws_security_group.jobs[0].id : null
}

output "lambda_function_arns" {
  description = "Lambda functions of the module."
  value       = { for k, f in aws_lambda_function.this : k => f.arn }
}

output "ecs_cluster_arn" {
  description = "ECS cluster used for Fargate jobs."
  value       = local.cluster_arn
}

output "task_definition_arns" {
  description = "Fargate task definitions per image key."
  value       = { for k, td in aws_ecs_task_definition.job : k => td.arn }
}

output "ssm_parameter_names" {
  description = "Task registrations per <project>/<taskKey>."
  value       = { for k, p in aws_ssm_parameter.task : k => p.name }
}

output "operator_policy_json" {
  description = "IAM policy for the operator role (CLI / CI): start, watch and stop runs, read reports, sync job sets. Not attached by the module."
  value       = jsonencode(local.operator_policy)
}

output "kms_key_policy_requirements" {
  description = "JSON list of statements the CMK key policy must allow when kms_key_arn is set (the module does not manage the key policy)."
  value = local.use_cmk ? jsonencode([
    {
      Sid       = "StepFunctionsAndLogs"
      Principal = { Service = ["states.amazonaws.com", "logs.${local.region}.amazonaws.com", "delivery.logs.amazonaws.com"] }
      Action    = ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    },
    {
      Sid       = "EventsToEncryptedQueueAndScheduler"
      Principal = { Service = ["events.amazonaws.com", "scheduler.amazonaws.com", "sns.amazonaws.com", "cloudwatch.amazonaws.com"] }
      Action    = ["kms:Decrypt", "kms:GenerateDataKey*"]
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    },
    {
      Sid       = "ModuleRoles"
      Principal = { AWS = concat([aws_iam_role.migrate.arn, aws_iam_role.post.arn, aws_iam_role.job_runner.arn, aws_iam_role.lambda_core.arn, aws_iam_role.lambda_waiter.arn, aws_iam_role.lambda_jobs.arn, aws_iam_role.ecs_execution.arn, aws_iam_role.ecs_task.arn, aws_iam_role.scheduler.arn], values(local.job_role_arns)) }
      Action    = ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
    },
  ]) : "[]"
}

output "network_contract" {
  description = "What the network must provide. The module creates no VPC endpoints (owned by the landing zone)."
  value = {
    subnets                  = var.subnet_ids
    jobs_security_group_id   = var.create_security_group ? aws_security_group.jobs[0].id : null
    egress                   = "443 to the VPC CIDR (interface endpoints) and the S3/DynamoDB prefix lists, or https_egress_*; database_ports to the databases"
    required_vpc_endpoints   = ["states", "dms", "secretsmanager", "kms", "logs", "ecr.api", "ecr.dkr", "s3 (gateway)", "dynamodb (gateway)", "ssm", "sns", "sqs", "sts"]
    optional_vpc_endpoints   = ["rds-data (Data API jobs)", "monitoring", "xray (enable_xray)"]
    not_needed_vpc_endpoints = ["lambda", "events", "ecs", "ecs-agent", "ecs-telemetry"]
  }
}

output "run_request_example" {
  description = "Example input for the migrate state machine."
  value = length(local.tasks) == 0 ? null : jsonencode({
    project     = values(local.tasks)[0].project
    taskKey     = values(local.tasks)[0].task_key
    operation   = "plan"
    requestedBy = "you@example.com"
  })
}

output "verify_commands" {
  description = "Commands to check the deployment without starting DMS."
  value = length(local.tasks) == 0 ? [] : [
    "aws stepfunctions start-execution --state-machine-arn ${aws_sfn_state_machine.run["migrate"].arn} --name plan-$(date +%s) --input '{\"project\":\"${values(local.tasks)[0].project}\",\"taskKey\":\"${values(local.tasks)[0].task_key}\",\"operation\":\"plan\"}'",
    "python scripts/orch.py --state-machine-arn ${aws_sfn_state_machine.run["migrate"].arn} start --project ${values(local.tasks)[0].project} --task ${values(local.tasks)[0].task_key} --dry-run --wait",
  ]
}
