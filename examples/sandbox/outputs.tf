output "region" {
  value = var.region
}

output "state_machine_arn" {
  value = module.dms_orchestration.state_machine_arns.migrate
}

output "post_state_machine_arn" {
  value = module.dms_orchestration.state_machine_arns.post
}

output "table_name" {
  value = module.dms_orchestration.table_name
}

output "artifacts_bucket" {
  value = module.dms_orchestration.artifacts_bucket
}

output "task_arn" {
  value = module.dms.task_arns["core"]
}

output "replication_instance_arn" {
  value = module.dms.replication_instance_arn
}

output "databases" {
  value = { for k, a in module.aurora : k => a.cluster_endpoint }
}

output "secrets" {
  value = { for k, s in aws_secretsmanager_secret.db : k => s.arn }
}

output "sql_lambda_arn" {
  value = aws_lambda_function.sql.arn
}

output "dms_event_rule" {
  value = "dms-orch-sandbox-dms-task-state" # module name "dms-orch" + environment
}

output "dms_source_password" {
  description = "Set on the dms_source role by scripts/sandbox.py seed."
  value       = random_password.db["dms_source"].result
  sensitive   = true
}

output "sns_topic_arn" {
  value = module.dms_orchestration.sns_topic_arn
}

output "dms_events_log_group" {
  value = aws_cloudwatch_log_group.dms_events.name
}

output "cluster_arns" {
  description = "Aurora cluster ARNs (Data API)."
  value       = { for k, a in module.aurora : k => a.cluster_arn }
}

output "endpoint_arns" {
  value = module.dms.endpoint_arns
}

output "dms_log_group" {
  value = module.dms.log_group_name
}

output "sql_runner_codebuild_project" {
  value = one(aws_codebuild_project.sql_runner[*].name)
}

output "sql_runner_repository" {
  value = one(aws_ecr_repository.sql_runner[*].name)
}

output "ssm_prefix" {
  value = trimsuffix(module.dms_orchestration.ssm_parameter_names["ledger/core"], "/ledger/core")
}

output "ssm_runner" {
  description = "SSM runner instance id (ssm_runner = true), else null: e2e s1ssm and s10 on SSM."
  value       = one(aws_instance.ssm_runner[*].id)
}

output "fanout_queue_url" {
  description = "S16 queue (sqs_fanout = true), else null."
  value       = var.sqs_fanout ? aws_sqs_queue.fanout[0].url : null
}
