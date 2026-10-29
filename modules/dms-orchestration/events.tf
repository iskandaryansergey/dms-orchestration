############################################
# DMS task state changes -> dms_event_handler -> post (ADR 0002, 0005)
############################################

resource "aws_cloudwatch_event_rule" "dms_task_state" {
  name        = "${local.prefix}-dms-task-state"
  description = "DMS replication task state changes in this account and region"
  event_pattern = jsonencode({
    source        = ["aws.dms"]
    "detail-type" = ["DMS Replication Task State Change"]
    resources     = [{ prefix = "arn:${local.partition}:dms:${local.region}:${local.account_id}:task:" }]
  })
  tags = local.tags
}

resource "aws_cloudwatch_event_target" "dms_task_state" {
  rule      = aws_cloudwatch_event_rule.dms_task_state.name
  target_id = "dms-event-handler"
  arn       = aws_lambda_function.this["dms_event_handler"].arn

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 10
  }

  dead_letter_config {
    arn = aws_sqs_queue.dlq.arn
  }
}

############################################
# migrate/post executions that end outside their own error handling -> release_lock (ADR 0004, 0005)
############################################

resource "aws_cloudwatch_event_rule" "execution_status" {
  name        = "${local.prefix}-execution-status"
  description = "Release DMS task locks of migrate/post executions that failed, timed out or were aborted"
  event_pattern = jsonencode({
    source        = ["aws.states"]
    "detail-type" = ["Step Functions Execution Status Change"]
    detail = {
      status          = ["FAILED", "TIMED_OUT", "ABORTED"]
      stateMachineArn = [local.migrate_arn, local.post_arn]
    }
  })
  tags = local.tags
}

resource "aws_cloudwatch_event_target" "execution_status" {
  rule      = aws_cloudwatch_event_rule.execution_status.name
  target_id = "release-lock"
  arn       = aws_lambda_function.this["release_lock"].arn

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 10
  }

  dead_letter_config {
    arn = aws_sqs_queue.dlq.arn
  }
}

############################################
# Dead-letter queue for undeliverable events and failed async invocations
############################################

resource "aws_sqs_queue" "dlq" {
  name                              = "${local.prefix}-events-dlq"
  message_retention_seconds         = 1209600
  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = local.use_cmk ? 300 : null
  sqs_managed_sse_enabled           = local.use_cmk ? null : true
  tags                              = local.tags
}

resource "aws_sqs_queue_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowEventBridgeDeadLetters"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.dlq.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = [aws_cloudwatch_event_rule.dms_task_state.arn, aws_cloudwatch_event_rule.execution_status.arn]
          }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:*"
        Resource  = aws_sqs_queue.dlq.arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
}

############################################
# Watchdog schedule
############################################

resource "aws_scheduler_schedule" "watchdog" {
  name                = "${local.prefix}-watchdog"
  description         = "DMS orchestration watchdog: lost DMS events, stale claims, orphaned locks, overdue runs"
  schedule_expression = "rate(${var.watchdog_interval_minutes} minutes)"
  kms_key_arn         = var.kms_key_arn

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.this["watchdog"].arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "scheduler" })

    retry_policy {
      maximum_event_age_in_seconds = 300
      maximum_retry_attempts       = 2
    }
  }
}
