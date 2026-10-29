############################################
# Alarms -> SNS topic
############################################

locals {
  alarm_actions       = [local.topic_arn]
  lambda_error_alarms = var.create_alarms ? toset(["dms_event_handler", "watchdog", "release_lock"]) : toset([])
  sfn_alarm_metrics = var.create_alarms ? merge([
    for m in ["migrate", "post"] : {
      "${m}-failed"    = { metric = "ExecutionsFailed", arn = aws_sfn_state_machine.run[m].arn, machine = m }
      "${m}-timed-out" = { metric = "ExecutionsTimedOut", arn = aws_sfn_state_machine.run[m].arn, machine = m }
      "${m}-aborted"   = { metric = "ExecutionsAborted", arn = aws_sfn_state_machine.run[m].arn, machine = m }
    }
  ]...) : {}
}

resource "aws_cloudwatch_metric_alarm" "sfn" {
  for_each            = local.sfn_alarm_metrics
  alarm_name          = "${local.prefix}-${each.key}"
  alarm_description   = "DMS orchestration: ${each.value.metric} on the ${each.value.machine} state machine"
  namespace           = "AWS/States"
  metric_name         = each.value.metric
  dimensions          = { StateMachineArn = each.value.arn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = local.lambda_error_alarms
  alarm_name          = "${local.prefix}-${each.key}-errors"
  alarm_description   = "DMS orchestration: errors in ${each.key}"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.this[each.key].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "dlq" {
  count               = var.create_alarms ? 1 : 0
  alarm_name          = "${local.prefix}-events-dlq"
  alarm_description   = "DMS orchestration: undelivered DMS / execution events or failed async invocations"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  tags                = local.tags
}

# "The watchdog is dead": no invocations for 3 intervals. Missing data is breaching.
resource "aws_cloudwatch_metric_alarm" "watchdog_silent" {
  count               = var.create_alarms ? 1 : 0
  alarm_name          = "${local.prefix}-watchdog-not-running"
  alarm_description   = "DMS orchestration: the watchdog has not run; lost DMS events would not be recovered"
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.this["watchdog"].function_name }
  statistic           = "Sum"
  period              = var.watchdog_interval_minutes * 60 * 3
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "orphans" {
  for_each            = var.create_alarms ? toset(["MigrationOverdue", "OrphanedLock"]) : toset([])
  alarm_name          = "${local.prefix}-${lower(each.key)}"
  alarm_description   = "DMS orchestration watchdog: ${each.key} (a DMS run took longer than its runTimeoutSeconds, or an execution ended without its own cleanup)"
  namespace           = "DmsOrchestration"
  metric_name         = each.key
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  tags                = local.tags
}
