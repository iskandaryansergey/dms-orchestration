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
    condition     = jsondecode(aws_sfn_state_machine.job_runner[0].definition).States.RunFargateJob.Arguments.Cluster == var.fargate.cluster_arn
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
    condition     = jsonencode([for c in jsondecode(output.bucket_requirements) : c.Control]) == jsonencode(["Versioning", "DefaultEncryption", "DenyInsecureTransport", "DenyDumpsWithoutKms", "Lifecycle"])
    error_message = "bucket_requirements lists the five controls a brought bucket must have"
  }

  assert {
    condition = alltrue([
      one([for c in jsondecode(output.bucket_requirements) : c.Status if c.Control == "Versioning"]) == "Enabled",
      one([for c in jsondecode(output.bucket_requirements) : c.SSEAlgorithm if c.Control == "DefaultEncryption"]) == "aws:kms",
      one([for c in jsondecode(output.bucket_requirements) : c.Statement.Condition.Bool["aws:SecureTransport"] if c.Control == "DenyInsecureTransport"]) == "false",
      jsonencode(one([for c in jsondecode(output.bucket_requirements) : c.Statement.Resource if c.Control == "DenyDumpsWithoutKms"])) == jsonencode(["arn:aws:s3:::platform-dms-artifacts/dumps/*"]),
    ])
    error_message = "the controls name the caller's bucket: versioning on, SSE-KMS, TLS only, dumps only with SSE-KMS"
  }

  assert {
    condition = alltrue([
      one([for r in one([for c in jsondecode(output.bucket_requirements) : c.Rules if c.Control == "Lifecycle"]) : r.Expiration.Days if r.Filter.Prefix == "runs/"]) == 365,
      one([for r in one([for c in jsondecode(output.bucket_requirements) : c.Rules if c.Control == "Lifecycle"]) : r.Expiration.Days if r.Filter.Prefix == "dumps/"]) == 14,
      one([for r in one([for c in jsondecode(output.bucket_requirements) : c.Rules if c.Control == "Lifecycle"]) : r.NoncurrentVersionExpiration.NoncurrentDays if r.Filter.Prefix == "jobs/"]) == 90,
      one([for r in one([for c in jsondecode(output.bucket_requirements) : c.Rules if c.Control == "Lifecycle"]) : r.AbortIncompleteMultipartUpload.DaysAfterInitiation if r.Filter.Prefix == ""]) == 7,
    ])
    error_message = "lifecycle rules carry the module's retention values for runs/, dumps/ and jobs/ and abort incomplete uploads bucket-wide"
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
    condition     = aws_dynamodb_table.this[0].server_side_encryption[0].kms_key_arn == var.kms_key_arn && aws_sqs_queue.dlq[0].kms_master_key_id == var.kms_key_arn
    error_message = "table and DLQ encrypted with the CMK"
  }

  assert {
    condition     = one([for r in aws_s3_bucket_server_side_encryption_configuration.artifacts[0].rule : r.apply_server_side_encryption_by_default[0].sse_algorithm]) == "aws:kms"
    error_message = "bucket uses SSE-KMS"
  }

  assert {
    condition     = length(jsondecode(output.kms_key_policy_statements)) == 3 && length(jsondecode(output.kms_key_policy_requirements)) == 4
    error_message = "key policy requirements are published"
  }

  assert {
    condition     = aws_sns_topic.this[0].kms_master_key_id == var.kms_key_arn
    error_message = "with a CMK the topic is encrypted with it"
  }

  assert {
    condition     = contains(one([for st in jsondecode(output.kms_key_policy_statements) : st.Principal.Service if st.Sid == "EventsToEncryptedQueueAndScheduler"]), "cloudwatch.amazonaws.com")
    error_message = "the CMK key policy must let CloudWatch alarms publish to the encrypted topic"
  }

  assert {
    condition     = contains(one([for st in jsondecode(output.kms_key_policy_statements) : st.Principal.AWS if st.Sid == "ModuleRoles"]), "arn:aws:iam::111122223333:role/dms-orch-stage-job-ledger")
    error_message = "project job roles must be allowed to use the CMK"
  }

  assert {
    condition     = length([for st in jsondecode(aws_iam_role_policy.lambda_waiter[0].policy).Statement : st if st.Sid == "UseOrchestratorKey"]) == 1
    error_message = "roles may use the CMK"
  }

  assert {
    condition     = aws_lambda_function.this["dms_event_handler"].reserved_concurrent_executions == 5 && aws_lambda_function.this["resolve_context"].reserved_concurrent_executions == -1
    error_message = "reserved concurrency only for the event handler and watchdog"
  }
}

# Fargate is the optional third runtime (ADR 0011): images turn on the cluster, the task
# definitions, the ECS roles and the ECR endpoints, all of which are absent by default.
run "fargate_optional" {
  command = apply

  assert {
    condition     = length(aws_ecs_cluster.this) == 1 && output.ecs_cluster_arn == aws_ecs_cluster.this[0].arn
    error_message = "fargate.images without cluster_arn creates the cluster and exports its ARN"
  }

  assert {
    condition     = keys(aws_ecs_task_definition.job) == ["sql-runner"] && length(aws_iam_role.ecs_execution) == 1 && length(aws_iam_role.ecs_task) == 1
    error_message = "one task definition per image and the ECS execution and task roles"
  }

  assert {
    condition     = contains(output.network_contract.required_vpc_endpoints, "ecr.api") && contains(output.network_contract.required_vpc_endpoints, "ecr.dkr")
    error_message = "Fargate pulls the image through the ECR endpoints"
  }

  assert {
    condition     = strcontains(aws_iam_role.job["ledger"].assume_role_policy, "EcsTasksOfThisAccount") && jsondecode(aws_ssm_parameter.task["ledger/core"].value).runtimes.fargate == true
    error_message = "job roles trust ECS tasks and the registration lists the Fargate runtime"
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

run "sql_jobs_off_and_no_images_without_warnings" {
  command = apply

  variables {
    lambda  = { sql_jobs_enabled = false }
    fargate = {}
  }

  # No sql_jobs function, no route to it, nothing to invoke; no image is not a warning any more.
  assert {
    condition = (
      !contains(keys(aws_lambda_function.this), "sql_jobs")
      && aws_lambda_function.this["prepare_job"].environment[0].variables.SQL_JOBS_ARN == ""
      && !anytrue([for r in flatten([for st in jsondecode(aws_iam_role_policy.job_runner[0].policy).Statement : st.Resource if st.Sid == "InvokeJobLambdas"]) : strcontains(r, "sql_jobs")])
    )
    error_message = "lambda.sql_jobs_enabled = false removes the function and its routes"
  }
}

run "platform_checks_outside_the_sandbox" {
  command = plan

  variables {
    acknowledged_checks = ["no_cmk", "include_execution_data", "lambda_reserved_concurrency", "dumps_without_cmk"]
  }

  # stage without the platform topic and with state in the DMS workspace: both advisories fire
  expect_failures = [check.own_sns_topic_outside_sandbox, check.state_in_the_dms_workspace]
}

# SSM runtime with a document the platform registers itself (ssm.document_name_override): the
# module ships the runtime zip and the content, the job-runner may send only that document.
run "ssm_runtime_with_a_platform_document" {
  command = apply

  variables {
    kms_key_arn = "arn:aws:kms:eu-west-2:111122223333:key/11111111-2222-3333-4444-555555555555"
    ssm = {
      enabled                = true
      target_tags            = { Role = "dms-runner" }
      instance_role_arn      = "arn:aws:iam::111122223333:role/team-dms-runner"
      document_name_override = "platform-dms-orch-job"
      runner_user            = "dmsjob"
    }
  }

  assert {
    condition     = length(aws_ssm_document.orch_job) == 0 && output.ssm_document_name == "platform-dms-orch-job"
    error_message = "with document_name_override the module creates no document"
  }

  assert {
    condition     = one([for st in jsondecode(aws_iam_role_policy.job_runner[0].policy).Statement : st.Resource if st.Sid == "SendJobDocument"]) == ["arn:aws:ssm:eu-west-2:111122223333:document/platform-dms-orch-job"]
    error_message = "the job-runner sends only the named document"
  }

  assert {
    condition     = strcontains(output.ssm_document_content, aws_s3_object.ssm_runtime[0].key) && strcontains(output.ssm_document_content, "runner_user='dmsjob'")
    error_message = "the content to register names the uploaded runtime zip and the runner user"
  }

  assert {
    condition     = aws_s3_object.ssm_runtime[0].server_side_encryption == "aws:kms" && aws_s3_object.ssm_runtime[0].kms_key_id == var.kms_key_arn
    error_message = "the runtime zip is encrypted with the CMK"
  }

  assert {
    condition     = length([for st in jsondecode(output.ssm_runner_policy_json).Statement : st if st.Sid == "UseOrchestratorKey"]) == 1
    error_message = "the runner instance profile can decrypt the runtime zip and the job objects"
  }
}

run "ssm_runtime_needs_runners_and_the_instance_role" {
  command = plan

  variables {
    ssm = { enabled = true }
  }

  expect_failures = [var.ssm]
}

run "ssm_runtime_never_runs_jobs_as_root" {
  command = plan

  variables {
    ssm = {
      enabled           = true
      instance_ids      = ["i-0123456789abcdef0"]
      instance_role_arn = "arn:aws:iam::111122223333:role/team-dms-runner"
      runner_user       = "root"
    }
  }

  expect_failures = [var.ssm]
}
