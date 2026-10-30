############################################
# Advisory checks (warnings, not errors). Silence with acknowledged_checks.
############################################

check "no_cmk" {
  assert {
    condition     = var.kms_key_arn != null ? true : contains(var.acknowledged_checks, "no_cmk")
    error_message = "kms_key_arn is not set: DynamoDB, S3, SNS, SQS, logs and Step Functions use AWS managed keys. Fine for a sandbox; set a CMK elsewhere or acknowledge 'no_cmk'."
  }
}

check "include_execution_data" {
  assert {
    condition     = var.include_execution_data ? contains(var.acknowledged_checks, "include_execution_data") : true
    error_message = "include_execution_data = true writes state input/output (ARNs, table names, job summaries; never secrets) to CloudWatch Logs. Acknowledge 'include_execution_data' or set it to false."
  }
}

check "fargate_image_tags" {
  assert {
    condition = alltrue([
      for image in values(var.fargate.images) :
      length(regexall(":latest$", image)) == 0 ? length(regexall(":[^/]+$", image)) > 0 : false
    ])
    error_message = "fargate.images must use an immutable tag (git SHA or semver), never ':latest' or no tag."
  }
}

check "lambda_reserved_concurrency" {
  assert {
    condition     = var.lambda.reserved_concurrency != null ? true : contains(var.acknowledged_checks, "lambda_reserved_concurrency")
    error_message = "lambda.reserved_concurrency is not set: the DMS event handler and watchdog share the account concurrency pool. Set it (e.g. 5) unless the account limit is too low (sandbox), or acknowledge 'lambda_reserved_concurrency'."
  }
}

check "own_sns_topic_outside_sandbox" {
  assert {
    condition     = var.environment == "sandbox" || var.sns_topic_arn != "" || contains(var.acknowledged_checks, "own_sns_topic_outside_sandbox")
    error_message = "sns_topic_arn is empty outside the sandbox: alerts go only to the platform SNS topic (fan-out by message attributes). Pass its ARN or acknowledge 'own_sns_topic_outside_sandbox'."
  }
}

check "state_in_the_dms_workspace" {
  assert {
    condition     = var.environment == "sandbox" || (var.table_name != "" && var.artifacts_bucket_name != "") || contains(var.acknowledged_checks, "state_in_the_dms_workspace")
    error_message = "table_name / artifacts_bucket_name are empty outside the sandbox: destroying the DMS workspace would take locks, runs, baselines and dumps with it. Bring them from a long-lived workspace (the bucket with the controls of the bucket_requirements output, e.g. modules/artifacts-bucket) or acknowledge 'state_in_the_dms_workspace'."
  }
}

check "dumps_without_cmk" {
  assert {
    condition     = var.environment == "sandbox" || var.kms_key_arn != null || contains(var.acknowledged_checks, "dumps_without_cmk")
    error_message = "Target dumps (dumps/) are encrypted with the AWS managed S3 key: set kms_key_arn outside the sandbox or acknowledge 'dumps_without_cmk'."
  }
}
