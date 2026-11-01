output "state_machine_arns" {
  description = "Step Functions state machines: migrate (start runs here), post (started by the DMS event; operation=post repeats it) and job_runner (internal)."
  value = local.create ? {
    migrate    = aws_sfn_state_machine.run["migrate"].arn
    post       = aws_sfn_state_machine.run["post"].arn
    job_runner = aws_sfn_state_machine.job_runner[0].arn
  } : {}
}

output "table_name" {
  description = "DynamoDB table with locks, runs, migrations, target baselines, events and job results."
  value       = local.create ? local.table_name : null
}

output "table_arn" {
  description = "ARN of the DynamoDB table (for an apply-time lock check in the DMS workspace)."
  value       = local.create ? local.table_arn : null
}

output "artifacts_bucket" {
  description = "Bucket for job sets (jobs/<project>/<version>/) and run artifacts (runs/<project>/<runId>/)."
  value       = local.create ? local.bucket_name : null
}

output "sns_topic_arn" {
  description = "Topic for run notifications and alarms."
  value       = local.topic_arn
}

output "jobs_security_group_id" {
  description = "Security group of Lambda and Fargate jobs. Add it to the Aurora ingress rules (upstream aurora module: ingress_rules.referenced_security_group_id)."
  value       = local.create_security_group ? aws_security_group.jobs[0].id : null
}

output "lambda_function_arns" {
  description = "Lambda functions of the module."
  value       = { for k, f in aws_lambda_function.this : k => f.arn }
}

output "ecs_cluster_arn" {
  description = "ECS cluster used for Fargate jobs (the module's or fargate.cluster_arn). null when fargate.images is empty: Fargate is an optional runtime."
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
  description = "IAM policy for the operator role (CLI / CI): start, watch and stop runs, read reports, restore under the task lock. No job set uploads (jobset_publisher_policy_json). Not attached by the module."
  value       = local.create ? jsonencode(local.operator_policy) : null
}

output "jobset_publisher_policy_json" {
  description = "IAM policy per project for whoever publishes that project's job sets (scripts/sync-jobs.sh): write and list jobs/<project>/ only. Not attached by the module."
  value       = { for p, pol in local.jobset_publisher_policies : p => jsonencode(pol) }
}

output "ssm_runner_policy_json" {
  description = "IAM policy for the instance profile role of the SSM runners (ssm.instance_role_arn), besides SSM management: read the runtime zip, assume the project job roles, write command output to the jobs log group. Not attached by the module. null when ssm.enabled is false."
  value       = local.ssm_enabled ? jsonencode(local.ssm_runner_policy) : null
}

output "ssm_document_content" {
  description = "Content (JSON) of the SSM Command document the job-runner sends; register it under ssm.document_name_override when the module does not create the document. null when ssm.enabled is false."
  value       = local.ssm_enabled ? jsonencode(local.ssm_document) : null
}

output "ssm_document_name" {
  description = "SSM document the job-runner sends for runtime ssm jobs (the module's <name>-<environment>-orch-job or ssm.document_name_override). null when ssm.enabled is false."
  value       = local.ssm_enabled ? local.ssm_document_name : null
}

locals {
  # KMS key policy contract v2 (one item per need): principals by kind; roles are symbolic
  # and resolved by the key module (deployer = the identity that applies this module,
  # module = the module's roles, listed in arns). The v1 statements are built from it.
  kms_requirements = !(local.create && local.use_cmk) ? [] : [
    {
      sid        = "StepFunctionsAndLogs"
      principals = { services = ["states.amazonaws.com", "logs.${local.region}.amazonaws.com", "delivery.logs.amazonaws.com"], arns = [], roles = [] }
      actions    = ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
      conditions = { StringEquals = { "aws:SourceAccount" = local.account_id } }
      purpose    = "Step Functions state and execution data, CloudWatch Logs groups and vended log delivery"
    },
    {
      sid        = "EventsToEncryptedQueueAndScheduler"
      principals = { services = ["events.amazonaws.com", "scheduler.amazonaws.com", "sns.amazonaws.com", "cloudwatch.amazonaws.com"], arns = [], roles = [] }
      actions    = ["kms:Decrypt", "kms:GenerateDataKey*"]
      conditions = { StringEquals = { "aws:SourceAccount" = local.account_id } }
      purpose    = "EventBridge dead letters to the encrypted queue, the watchdog schedule, CloudWatch alarms to the encrypted topic"
    },
    {
      sid = "ModuleRoles"
      principals = {
        services = []
        arns     = concat([aws_iam_role.migrate[0].arn, aws_iam_role.post[0].arn, aws_iam_role.job_runner[0].arn, aws_iam_role.lambda_core[0].arn, aws_iam_role.lambda_waiter[0].arn, aws_iam_role.lambda_jobs[0].arn, aws_iam_role.scheduler[0].arn], aws_iam_role.ecs_execution[*].arn, aws_iam_role.ecs_task[*].arn, values(local.job_role_arns))
        roles    = ["module"]
      }
      actions    = ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
      conditions = {}
      purpose    = "The module's roles and the project job roles read and write encrypted state, artifacts, dumps and messages"
    },
    {
      sid        = "Deployer"
      principals = { services = [], arns = [], roles = ["deployer"] }
      actions    = ["kms:DescribeKey", "kms:CreateGrant", "kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
      conditions = { StringEquals = { "kms:CallerAccount" = local.account_id } }
      purpose    = "Terraform creates the encrypted resources (DynamoDB, Lambda environment, Step Functions, SQS, SNS, log groups, the SSM runtime zip)"
    },
  ]

  # v1: key policy statements with concrete principals only (no symbolic-only items).
  kms_statements_v1 = [
    for i in local.kms_requirements : merge(
      {
        Sid = i.sid
        Principal = merge(
          length(i.principals.services) > 0 ? { Service = i.principals.services } : {},
          length(i.principals.arns) > 0 ? { AWS = i.principals.arns } : {},
        )
        Action = i.actions
      },
      length(i.conditions) > 0 ? { Condition = i.conditions } : {},
    ) if length(i.principals.services) + length(i.principals.arns) > 0
  ]
}

output "kms_key_policy_requirements" {
  description = "KMS key policy contract v2 (JSON list) when kms_key_arn is set: one item per need with sid, principals {services, arns, roles}, actions, conditions and purpose; roles are symbolic (deployer, module). The module does not manage the key policy."
  value       = jsonencode(local.kms_requirements)
}

output "kms_key_policy_statements" {
  description = "The same requirements as key policy statements (v1: Sid, Principal, Action, Condition) with concrete principals only; the deployer item of the v2 contract is left out."
  value       = jsonencode(local.kms_statements_v1)
}

output "bucket_requirements" {
  description = "JSON list of the controls the artifacts bucket must have when artifacts_bucket_name is set (the module does not manage a brought bucket): versioning, SSE-KMS default encryption, TLS only, DenyDumpsWithoutKms, lifecycle of runs/, jobs/ and dumps/. modules/artifacts-bucket creates a bucket that has them."
  value = !local.create ? null : jsonencode([
    {
      Control = "Versioning"
      Status  = "Enabled"
      Why     = "post loads context.json by the version id migrate recorded; job sets are published create-only"
    },
    {
      Control          = "DefaultEncryption"
      SSEAlgorithm     = "aws:kms"
      KMSMasterKeyID   = local.kms_key_arn # null: the AWS managed key aws/s3
      BucketKeyEnabled = true
    },
    {
      Control   = "DenyInsecureTransport"
      Statement = local.bucket_policy_statements.DenyInsecureTransport
    },
    {
      Control   = "DenyDumpsWithoutKms"
      Statement = local.bucket_policy_statements.DenyDumpsWithoutKms
    },
    {
      Control = "Lifecycle"
      Rules   = local.bucket_lifecycle_rules
    },
  ])
}

output "network_contract" {
  description = "What the network must provide. The module creates no VPC endpoints (owned by the landing zone)."
  value = !local.create ? null : {
    subnets                = var.subnet_ids
    jobs_security_group_id = local.create_security_group ? aws_security_group.jobs[0].id : null
    egress                 = "443 to the VPC CIDR (interface endpoints) and the S3/DynamoDB prefix lists, or https_egress_*; database_ports to the databases"
    # rds: dump.method = snapshot calls the RDS API from the jobs (defaults.snapshot_dumps).
    required_vpc_endpoints = concat(
      ["states", "dms", "secretsmanager", "kms", "logs", "s3 (gateway)", "dynamodb (gateway)", "ssm", "sns", "sqs", "sts"],
      # Fargate runtime (fargate.images): the tasks pull the image through ECR.
      local.fargate_enabled ? ["ecr.api", "ecr.dkr"] : [],
      anytrue([for t in values(local.tasks) : t.defaults.snapshot_dumps]) ? ["rds"] : [],
      # SSM runtime: the runner instances' agent (ssm is listed above already).
      local.ssm_enabled ? ["ec2messages", "ssmmessages"] : [],
    )
    optional_vpc_endpoints   = ["rds-data (Data API jobs)", "monitoring", "xray (enable_xray)"]
    not_needed_vpc_endpoints = concat(["lambda", "events", "ecs", "ecs-agent", "ecs-telemetry"], local.fargate_enabled ? [] : ["ecr.api", "ecr.dkr"])
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
