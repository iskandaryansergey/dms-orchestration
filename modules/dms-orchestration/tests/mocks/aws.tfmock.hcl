# Shared mock values for terraform test. Computed ARNs must look like ARNs because
# the provider validates them when they are passed on (alarm actions, task roles...).

mock_data "aws_region" {
  defaults = { name = "eu-west-2" }
}

mock_data "aws_caller_identity" {
  defaults = { account_id = "111122223333" }
}

mock_data "aws_partition" {
  defaults = { partition = "aws" }
}

mock_data "aws_vpc" {
  defaults = { cidr_block = "10.20.0.0/16" }
}

mock_data "aws_prefix_list" {
  defaults = { id = "pl-0a1b2c3d4e5f60718" }
}

mock_resource "aws_iam_role" {
  defaults = { arn = "arn:aws:iam::111122223333:role/mock-role" }
}

mock_resource "aws_sns_topic" {
  defaults = { arn = "arn:aws:sns:eu-west-2:111122223333:mock-topic" }
}

mock_resource "aws_sqs_queue" {
  defaults = {
    arn = "arn:aws:sqs:eu-west-2:111122223333:mock-dlq"
    id  = "https://sqs.eu-west-2.amazonaws.com/111122223333/mock-dlq"
  }
}

mock_resource "aws_lambda_function" {
  defaults = {
    arn        = "arn:aws:lambda:eu-west-2:111122223333:function:mock"
    invoke_arn = "arn:aws:apigateway:eu-west-2:lambda:path/2015-03-31/functions/arn:aws:lambda:eu-west-2:111122223333:function:mock/invocations"
  }
}

mock_resource "aws_sfn_state_machine" {
  defaults = { arn = "arn:aws:states:eu-west-2:111122223333:stateMachine:mock" }
}

mock_resource "aws_dynamodb_table" {
  defaults = { arn = "arn:aws:dynamodb:eu-west-2:111122223333:table/mock" }
}

mock_resource "aws_cloudwatch_log_group" {
  defaults = { arn = "arn:aws:logs:eu-west-2:111122223333:log-group:mock" }
}

mock_resource "aws_cloudwatch_event_rule" {
  defaults = { arn = "arn:aws:events:eu-west-2:111122223333:rule/mock" }
}

mock_resource "aws_ecs_cluster" {
  defaults = { arn = "arn:aws:ecs:eu-west-2:111122223333:cluster/mock" }
}

mock_resource "aws_ecs_task_definition" {
  defaults = { arn = "arn:aws:ecs:eu-west-2:111122223333:task-definition/mock:1" }
}

mock_resource "aws_s3_bucket" {
  defaults = { arn = "arn:aws:s3:::mock-bucket" }
}

mock_resource "aws_security_group" {
  defaults = { id = "sg-0123456789abcdef0" }
}

mock_resource "aws_ssm_parameter" {
  defaults = { arn = "arn:aws:ssm:eu-west-2:111122223333:parameter/mock" }
}

mock_resource "aws_scheduler_schedule" {
  defaults = { arn = "arn:aws:scheduler:eu-west-2:111122223333:schedule/default/mock" }
}
