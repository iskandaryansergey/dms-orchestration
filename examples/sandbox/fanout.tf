# Scenario S16: a subscriber of the run topic that receives only what its
# filter policy lets through. Off by default (sqs_fanout = false): nothing is created.
# The filter is on the SNS message attributes write_report sets (severity high = FAILED,
# MANUAL, ABORTED, TIMED_OUT); the same JSON is checked by tests/python/test_sandbox_helper.py.

resource "aws_sqs_queue" "fanout" {
  count                     = var.sqs_fanout ? 1 : 0
  name                      = "${var.name}-fanout"
  message_retention_seconds = 86400
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue_policy" "fanout" {
  count     = var.sqs_fanout ? 1 : 0
  queue_url = aws_sqs_queue.fanout[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "FromTheRunTopic"
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.fanout[0].arn
      Condition = { ArnEquals = { "aws:SourceArn" = module.dms_orchestration.sns_topic_arn } }
    }]
  })
}

resource "aws_sns_topic_subscription" "fanout" {
  count                = var.sqs_fanout ? 1 : 0
  topic_arn            = module.dms_orchestration.sns_topic_arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.fanout[0].arn
  raw_message_delivery = false
  filter_policy_scope  = "MessageAttributes"
  filter_policy        = file("${path.module}/fanout-filter-policy.json")

  depends_on = [aws_sqs_queue_policy.fanout]
}
