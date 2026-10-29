# Bring-your-own resources, customer managed key, advisory checks.

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  environment = "stage"
  vpc_id      = "vpc-0123456789abcdef0"
  subnet_ids  = ["subnet-aaaa1111", "subnet-bbbb2222"]
  dms = {
    replication_instance_arn = "arn:aws:dms:eu-west-2:111122223333:rep:RI1"
    replication_instance_id  = "dms-stage-ri"
    task_arns                = { core = "arn:aws:dms:eu-west-2:111122223333:task:CORE1" }
  }
  projects            = { ledger = { tasks = { core = { defaults = { run_timeout_seconds = 1209600 } } } } }
  fargate             = { images = { sql-runner = "111122223333.dkr.ecr.eu-west-2.amazonaws.com/dms-orch/sql-runner:0.1.0" } }
  acknowledged_checks = ["own_sns_topic_outside_sandbox", "state_in_the_dms_workspace", "dumps_without_cmk", "no_cmk", "include_execution_data", "lambda_reserved_concurrency"]
}

run "bring_your_own_bucket_topic_cluster_sg" {
  command = apply

  variables {
    artifacts_bucket_name = "platform-dms-artifacts"
    table_name            = "platform-dms-state"
    sns_topic_arn         = "arn:aws:sns:eu-west-2:111122223333:platform-alerts"
    create_security_group = false
    security_group_ids    = ["sg-0aaaabbbbccccdddd"]
    fargate = {
      cluster_arn = "arn:aws:ecs:eu-west-2:111122223333:cluster/platform"
      images      = { sql-runner = "111122223333.dkr.ecr.eu-west-2.amazonaws.com/dms-orch/sql-runner:0.1.0" }
    }
  }

  assert {
    condition     = length(aws_s3_bucket.artifacts) == 0 && length(aws_sns_topic.this) == 0 && length(aws_ecs_cluster.this) == 0 && length(aws_security_group.jobs) == 0
    error_message = "nothing the caller brings may be created again"
  }

  assert {
    condition     = length(aws_dynamodb_table.this) == 0 && aws_lambda_function.this["watchdog"].environment[0].variables.TABLE_NAME == "platform-dms-state" && jsondecode(aws_sfn_state_machine.run["migrate"].definition).States.AcquireLock.Arguments.TableName == "platform-dms-state"
    error_message = "the long-lived state table is used, not created"
  }

  assert {
    condition     = jsondecode(aws_sfn_state_machine.run["post"].definition).States.Notify.Arguments.TopicArn == var.sns_topic_arn
    error_message = "notifications go to the caller's topic"
  }

  assert {
    condition     = jsondecode(aws_sfn_state_machine.job_runner.definition).States.RunFargateJob.Arguments.Cluster == var.fargate.cluster_arn
    error_message = "jobs run in the caller's cluster"
  }

  assert {
    condition     = aws_lambda_function.this["write_report"].environment[0].variables.ARTIFACTS_BUCKET == "platform-dms-artifacts"
    error_message = "reports go to the caller's bucket"
  }

  assert {
    condition     = jsonencode(aws_lambda_function.this["watchdog"].vpc_config[0].security_group_ids) == jsonencode(toset(["sg-0aaaabbbbccccdddd"]))
    error_message = "Lambda uses the caller's security groups"
  }

  assert {
    condition     = jsondecode(aws_ssm_parameter.task["ledger/core"].value).defaults.runTimeoutSeconds == 1209600 && jsondecode(aws_sfn_state_machine.run["migrate"].definition).TimeoutSeconds == 604800
    error_message = "the per-task run timeout goes to the registration (MigrationOverdue), not to the execution timeout"
  }
}

run "customer_managed_key" {
  command = apply

  variables {
    kms_key_arn         = "arn:aws:kms:eu-west-2:111122223333:key/11111111-2222-3333-4444-555555555555"
    acknowledged_checks = ["own_sns_topic_outside_sandbox", "state_in_the_dms_workspace", "dumps_without_cmk", "include_execution_data", "lambda_reserved_concurrency"]
    lambda              = { reserved_concurrency = 5 }
  }

  assert {
    condition     = alltrue([for m in values(aws_sfn_state_machine.run) : m.encryption_configuration[0].type == "CUSTOMER_MANAGED_KMS_KEY"])
    error_message = "state machines encrypted with the CMK"
  }

  assert {
    condition     = aws_dynamodb_table.this[0].server_side_encryption[0].kms_key_arn == var.kms_key_arn && aws_sqs_queue.dlq.kms_master_key_id == var.kms_key_arn
    error_message = "table and DLQ encrypted with the CMK"
  }

  assert {
    condition     = one([for r in aws_s3_bucket_server_side_encryption_configuration.artifacts[0].rule : r.apply_server_side_encryption_by_default[0].sse_algorithm]) == "aws:kms"
    error_message = "bucket uses SSE-KMS"
  }

  assert {
    condition     = length(jsondecode(output.kms_key_policy_requirements)) == 3
    error_message = "key policy requirements are published"
  }

  assert {
    condition     = aws_sns_topic.this[0].kms_master_key_id == var.kms_key_arn
    error_message = "with a CMK the topic is encrypted with it"
  }

  assert {
    condition     = contains(one([for st in jsondecode(output.kms_key_policy_requirements) : st.Principal.Service if st.Sid == "EventsToEncryptedQueueAndScheduler"]), "cloudwatch.amazonaws.com")
    error_message = "the CMK key policy must let CloudWatch alarms publish to the encrypted topic"
  }

  assert {
    condition     = contains(one([for st in jsondecode(output.kms_key_policy_requirements) : st.Principal.AWS if st.Sid == "ModuleRoles"]), "arn:aws:iam::111122223333:role/dms-orch-stage-job-ledger")
    error_message = "project job roles must be allowed to use the CMK"
  }

  assert {
    condition     = length([for st in jsondecode(aws_iam_role_policy.lambda_waiter.policy).Statement : st if st.Sid == "UseOrchestratorKey"]) == 1
    error_message = "roles may use the CMK"
  }

  assert {
    condition     = aws_lambda_function.this["dms_event_handler"].reserved_concurrent_executions == 5 && aws_lambda_function.this["resolve_context"].reserved_concurrent_executions == -1
    error_message = "reserved concurrency only for the event handler and watchdog"
  }
}

run "advisory_checks_warn" {
  command = apply

  variables {
    acknowledged_checks    = []
    include_execution_data = true
    fargate                = { images = { sql-runner = "111122223333.dkr.ecr.eu-west-2.amazonaws.com/dms-orch/sql-runner:latest" } }
  }

  expect_failures = [
    check.no_cmk,
    check.include_execution_data,
    check.fargate_image_tags,
    check.lambda_reserved_concurrency,
    check.own_sns_topic_outside_sandbox,
    check.state_in_the_dms_workspace,
    check.dumps_without_cmk,
  ]
}

run "platform_checks_outside_the_sandbox" {
  command = plan

  variables {
    acknowledged_checks = ["no_cmk", "include_execution_data", "lambda_reserved_concurrency", "dumps_without_cmk"]
  }

  # stage without the platform topic and with state in the DMS workspace: both advisories fire
  expect_failures = [check.own_sns_topic_outside_sandbox, check.state_in_the_dms_workspace]
}
