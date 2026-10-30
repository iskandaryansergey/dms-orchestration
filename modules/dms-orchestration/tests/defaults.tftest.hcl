# Plan/apply under a mocked AWS provider: no credentials, nothing created.
# Mocked computed attributes are random, so the tests check the module's own logic
# (ASL, policies, SSM payloads, names, wiring), not provider behaviour.

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  environment = "sandbox"
  vpc_id      = "vpc-0123456789abcdef0"
  subnet_ids  = ["subnet-aaaa1111", "subnet-bbbb2222"]
  dms = {
    replication_instance_arn = "arn:aws:dms:eu-west-2:111122223333:rep:RI1"
    replication_instance_id  = "dms-sandbox-ri"
    task_arns                = { core = "arn:aws:dms:eu-west-2:111122223333:task:CORE1" }
    task_resource_ids        = { core = "CORE1" }
    endpoint_arns            = { source = "arn:aws:dms:eu-west-2:111122223333:endpoint:SRC", target = "arn:aws:dms:eu-west-2:111122223333:endpoint:TGT" }
    log_group_name           = "dms-tasks-dms-sandbox-ri"
  }
  job_secret_arns = ["arn:aws:secretsmanager:eu-west-2:111122223333:secret:dms/ledger/source/runner-AbCdEf"]
  projects = {
    ledger = {
      tasks = {
        core = {
          connections = {
            source = {
              engine     = "postgresql"
              secret_arn = "arn:aws:secretsmanager:eu-west-2:111122223333:secret:dms/ledger/source/runner-AbCdEf"
              host       = "source.cluster-xyz.eu-west-2.rds.amazonaws.com"
              port       = 5432
              database   = "ledger"
            }
          }
          jobs = { default_version = "v1", bundles = { v1 = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0" } }
        }
      }
    }
  }
  fargate = {
    images = { sql-runner = "111122223333.dkr.ecr.eu-west-2.amazonaws.com/dms-orch/sql-runner:0.1.0" }
  }
  database_ports       = [5432]
  database_cidr_blocks = ["10.0.16.0/20"]
  acknowledged_checks  = ["no_cmk", "include_execution_data", "lambda_reserved_concurrency"]
}

run "state_machines" {
  command = apply

  # ADR 0005: three machines, two entry points.
  assert {
    condition     = toset(keys(aws_sfn_state_machine.run)) == toset(["migrate", "post"])
    error_message = "run machines are migrate and post"
  }

  assert {
    condition     = alltrue([for m in concat(values(aws_sfn_state_machine.run), [aws_sfn_state_machine.job_runner]) : m.type == "STANDARD"])
    error_message = "state machines must be STANDARD (ADR 0001)"
  }

  # Review finding 9: state input/output (table names, job summaries, DMS messages)
  # stays out of CloudWatch Logs unless the caller opts in.
  assert {
    condition     = alltrue([for m in concat(values(aws_sfn_state_machine.run), [aws_sfn_state_machine.job_runner]) : !m.logging_configuration[0].include_execution_data])
    error_message = "execution data must not be logged by default"
  }

  assert {
    condition     = alltrue([for m in values(aws_sfn_state_machine.run) : jsondecode(m.definition).QueryLanguage == "JSONata"])
    error_message = "run machines must use JSONata (ADR 0003)"
  }

  assert {
    condition = alltrue([for s in ["ResolveContext", "AcquireLock", "InspectTask", "TargetVerify", "Preflight", "CheckVerdict", "PreJobs", "ResetJobs", "SaveContext", "Arm", "StartReplicationTask", "ReportStarted", "ClassifyFailure", "OnFailureJobs", "WriteReport", "ReleaseLock"] :
    contains(keys(jsondecode(aws_sfn_state_machine.run["migrate"].definition).States), s)])
    error_message = "migrate is missing a required state"
  }

  assert {
    condition = alltrue([for s in ["LoadRun", "TakeOverLock", "DeleteMigration", "PostJobs", "ClassifyFailure", "ResumeBaseline", "RecordResumeBaseline", "HandOverLock", "StartResume", "OnFailureJobs", "BaselineVerify", "WriteReport", "ReleaseLock"] :
    contains(keys(jsondecode(aws_sfn_state_machine.run["post"].definition).States), s)])
    error_message = "post is missing a required state"
  }

  assert {
    condition     = jsondecode(aws_sfn_state_machine.run["migrate"].definition).States.AcquireLock.Arguments.TableName == aws_dynamodb_table.this[0].name
    error_message = "the lock must live in the module table"
  }

  assert {
    condition     = jsondecode(aws_sfn_state_machine.run["migrate"].definition).States.StartPostDryRun.Arguments.StateMachineArn == "arn:aws:states:eu-west-2:111122223333:stateMachine:${aws_sfn_state_machine.run["post"].name}"
    error_message = "migrate hands a dry run to the post machine of this module"
  }

  assert {
    condition     = jsondecode(aws_sfn_state_machine.run["post"].definition).States.StartResume.Arguments.StateMachineArn == "arn:aws:states:eu-west-2:111122223333:stateMachine:${aws_sfn_state_machine.run["migrate"].name}"
    error_message = "post chains auto-resume to the migrate machine of this module"
  }

  assert {
    condition     = jsondecode(aws_sfn_state_machine.run["migrate"].definition).States.SaveContext.Arguments.Bucket == aws_s3_bucket.artifacts[0].bucket
    error_message = "migrate saves the run context in the module bucket"
  }

  assert {
    condition     = alltrue([for m in values(aws_sfn_state_machine.run) : jsondecode(m.definition).TimeoutSeconds == 604800])
    error_message = "migrate and post never wait for DMS: one week for their jobs"
  }

  assert {
    condition     = aws_cloudwatch_event_rule.execution_status.event_pattern == jsonencode({ source = ["aws.states"], "detail-type" = ["Step Functions Execution Status Change"], detail = { status = ["FAILED", "TIMED_OUT", "ABORTED"], stateMachineArn = [for m in ["migrate", "post"] : "arn:aws:states:eu-west-2:111122223333:stateMachine:${aws_sfn_state_machine.run[m].name}"] } })
    error_message = "release_lock listens to both run machines"
  }

  assert {
    condition     = aws_lambda_function.this["dms_event_handler"].environment[0].variables.POST_STATE_MACHINE_ARN == "arn:aws:states:eu-west-2:111122223333:stateMachine:${aws_sfn_state_machine.run["post"].name}" && aws_lambda_function.this["watchdog"].environment[0].variables.POST_STATE_MACHINE_ARN == "arn:aws:states:eu-west-2:111122223333:stateMachine:${aws_sfn_state_machine.run["post"].name}"
    error_message = "the handoff Lambdas start the post machine of this module"
  }

  assert {
    condition     = !contains(keys(aws_lambda_function.this), "register_waiter")
    error_message = "register_waiter is gone with the callback wait (ADR 0005)"
  }

  assert {
    condition     = jsonencode(jsondecode(aws_sfn_state_machine.job_runner.definition).States.RunFargateJob.Arguments.NetworkConfiguration.AwsvpcConfiguration.Subnets) == jsonencode(var.subnet_ids)
    error_message = "Fargate jobs must run in the module subnets"
  }

  assert {
    condition     = jsondecode(aws_sfn_state_machine.job_runner.definition).States.RunFargateJob.Arguments.NetworkConfiguration.AwsvpcConfiguration.AssignPublicIp == "DISABLED"
    error_message = "Fargate jobs get no public IP by default"
  }

  assert {
    condition     = aws_sfn_state_machine.run["migrate"].logging_configuration[0].level == "ALL" && startswith(aws_cloudwatch_log_group.sfn.name, "/aws/vendedlogs/states/")
    error_message = "state machine logs go to a /aws/vendedlogs/ group"
  }

  assert {
    condition     = length(aws_sfn_state_machine.run["post"].encryption_configuration) == 0
    error_message = "no CMK configured, so no customer managed encryption block"
  }
}

run "storage_and_registration" {
  command = apply

  assert {
    condition     = aws_dynamodb_table.this[0].hash_key == "pk" && aws_dynamodb_table.this[0].range_key == "sk"
    error_message = "single-table keys pk/sk"
  }

  assert {
    condition     = aws_dynamodb_table.this[0].point_in_time_recovery[0].enabled && aws_dynamodb_table.this[0].server_side_encryption[0].enabled
    error_message = "PITR and encryption must be on"
  }

  assert {
    condition     = toset([for g in aws_dynamodb_table.this[0].global_secondary_index : g.name]) == toset(["gsi1", "gsi2"])
    error_message = "gsi1 (history) and gsi2 (active waiters/locks) are required by the Lambdas"
  }

  assert {
    condition     = aws_ssm_parameter.task["ledger/core"].name == "/dms-orch/sandbox/ledger/core" && aws_ssm_parameter.task["ledger/core"].type == "String"
    error_message = "registration parameter name/type"
  }

  # Tables in error fail the run unless the task opts out.
  assert {
    condition     = jsondecode(aws_ssm_parameter.task["ledger/core"].value).defaults.failOnTableErrors == true
    error_message = "fail_on_table_errors defaults to true"
  }

  # Tables DMS did not validate fail the run unless the task opts out.
  assert {
    condition     = jsondecode(aws_ssm_parameter.task["ledger/core"].value).defaults.failOnValidationErrors == true
    error_message = "fail_on_validation_errors defaults to true"
  }

  assert {
    condition     = jsondecode(aws_ssm_parameter.task["ledger/core"].value).taskArn == var.dms.task_arns.core
    error_message = "registration must carry the task ARN from the DMS module"
  }

  assert {
    condition     = jsondecode(aws_ssm_parameter.task["ledger/core"].value).connections.source.secretArn == var.job_secret_arns[0]
    error_message = "connections carry the secret ARN"
  }

  assert {
    condition     = length(regexall("(?i)password|secretstring", aws_ssm_parameter.task["ledger/core"].value)) == 0
    error_message = "the registration must not contain secret values"
  }

  assert {
    condition     = jsondecode(aws_ssm_parameter.task["ledger/core"].value).jobs.s3Prefix == "jobs/ledger"
    error_message = "default job set prefix is jobs/<project>"
  }

  assert {
    condition = (
      jsondecode(aws_ssm_parameter.task["ledger/core"].value).jobs.bundles == { v1 = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0" }
      && jsondecode(aws_ssm_parameter.task["ledger/core"].value).jobs.allowUnpinned == false
    )
    error_message = "the registration carries the pinned bundle hashes; unpinned job sets are refused by default"
  }

  assert {
    condition     = aws_s3_bucket_public_access_block.artifacts[0].block_public_acls && aws_s3_bucket_public_access_block.artifacts[0].restrict_public_buckets
    error_message = "artifacts bucket must block public access"
  }

  assert {
    condition     = length(aws_sns_topic.this) == 1 && length(aws_s3_bucket.artifacts) == 1
    error_message = "topic and bucket are created by default"
  }

  # Review finding 5: CloudWatch alarms cannot publish to a topic encrypted with the
  # AWS managed key alias/aws/sns (its key policy cannot be changed).
  assert {
    condition     = aws_sns_topic.this[0].kms_master_key_id == null
    error_message = "without a CMK the topic must not use alias/aws/sns, or alarms never arrive"
  }
}

run "lambda_and_events" {
  command = apply

  assert {
    condition     = length(aws_lambda_function.this) == 10
    error_message = "10 Lambda functions expected (register_waiter is gone, sql_jobs on by default)"
  }

  # Lambda SQL runtime (ADR 0011): the default for sql: jobs, the full Lambda timeout, more memory.
  assert {
    condition = (
      aws_lambda_function.this["sql_jobs"].timeout == 900
      && aws_lambda_function.this["sql_jobs"].memory_size == 1024
      && aws_lambda_function.this["sql_jobs"].handler == "lambdas.sql_jobs.handler.handler"
      && aws_lambda_function.this["sql_jobs"].role == aws_iam_role.lambda_jobs.arn
      && aws_lambda_function.this["builtin_jobs"].memory_size == 512
    )
    error_message = "sql_jobs: 900 s, 1024 MB, the jobs role; the others keep lambda.memory_mb"
  }

  assert {
    condition     = endswith(aws_lambda_function.this["prepare_job"].environment[0].variables.SQL_JOBS_ARN, ":function:dms-orch-sandbox-sql_jobs")
    error_message = "prepare_job routes sql jobs to the sql_jobs function"
  }

  assert {
    condition = (
      contains(flatten([for st in jsondecode(aws_iam_role_policy.job_runner.policy).Statement : st.Resource if st.Sid == "InvokeJobLambdas"]), aws_lambda_function.this["sql_jobs"].arn)
      && contains(flatten([for st in jsondecode(aws_iam_role_policy.lambda_jobs.policy).Statement : st.Resource if st.Sid == "Logs"]), "${aws_cloudwatch_log_group.lambda["sql_jobs"].arn}:*")
    )
    error_message = "the job runner invokes sql_jobs; sql_jobs logs to its own group with the jobs role"
  }

  assert {
    condition     = alltrue([for f in aws_lambda_function.this : f.runtime == "python3.13" && length(f.vpc_config[0].subnet_ids) == 2])
    error_message = "all Lambda functions run python3.13 inside the VPC"
  }

  assert {
    condition     = aws_lambda_function.this["resolve_context"].environment[0].variables.SSM_PREFIX == "/dms-orch/sandbox"
    error_message = "resolve_context reads registrations under /<name>/<environment>"
  }

  assert {
    condition     = jsondecode(aws_cloudwatch_event_rule.dms_task_state.event_pattern).source == ["aws.dms"]
    error_message = "DMS rule listens to aws.dms"
  }

  assert {
    condition     = jsondecode(aws_cloudwatch_event_rule.execution_status.event_pattern).detail.status == ["FAILED", "TIMED_OUT", "ABORTED"]
    error_message = "lock safety net on FAILED/TIMED_OUT/ABORTED"
  }

  assert {
    condition     = aws_scheduler_schedule.watchdog.schedule_expression == "rate(5 minutes)"
    error_message = "watchdog every 5 minutes by default"
  }

  assert {
    condition     = aws_cloudwatch_event_target.dms_task_state.dead_letter_config[0].arn == aws_sqs_queue.dlq.arn
    error_message = "undeliverable DMS events go to the DLQ"
  }

  assert {
    condition     = length(aws_vpc_security_group_egress_rule.db_cidr) == 1 && aws_vpc_security_group_egress_rule.https_cidr["10.20.0.0/16"].from_port == 443
    error_message = "jobs SG egress: 443 + database port per CIDR"
  }

  assert {
    condition     = keys(aws_vpc_security_group_egress_rule.https_cidr) == ["10.20.0.0/16"] && length(aws_vpc_security_group_egress_rule.https_prefix_list) == 2
    error_message = "jobs SG 443 egress defaults to the VPC CIDR (interface endpoints) and the S3/DynamoDB gateway prefix lists, never 0.0.0.0/0"
  }

  assert {
    condition     = alltrue([for c in jsondecode(aws_ecs_task_definition.job["sql-runner"].container_definitions) : c.readonlyRootFilesystem == true])
    error_message = "Fargate job containers use a read-only root file system"
  }

  # The pre-check checks/connections runs TestConnection itself,
  # only on this replication instance and the registered endpoints.
  assert {
    condition = toset(flatten([one([for st in jsondecode(aws_iam_role_policy.lambda_jobs.policy).Statement : st.Resource if st.Sid == "TestEndpointConnections"])])) == toset([
      "arn:aws:dms:eu-west-2:111122223333:rep:RI1",
      "arn:aws:dms:eu-west-2:111122223333:endpoint:SRC",
      "arn:aws:dms:eu-west-2:111122223333:endpoint:TGT",
    ]) && one([for st in jsondecode(aws_iam_role_policy.lambda_jobs.policy).Statement : st.Action if st.Sid == "TestEndpointConnections"]) == ["dms:TestConnection"]
    error_message = "built-in jobs may run TestConnection on the registered endpoints only"
  }

  # Fargate mounts task volumes empty, root:root 0755, without the image content (seen live, S1f):
  # a short root init container opens them to uid 10001 before the job container starts.
  assert {
    condition = (
      [for c in jsondecode(aws_ecs_task_definition.job["sql-runner"].container_definitions) : c.name] == ["init-volumes", "job"]
      && one([for c in jsondecode(aws_ecs_task_definition.job["sql-runner"].container_definitions) : c if c.name == "init-volumes"]).user == "0"
      && one([for c in jsondecode(aws_ecs_task_definition.job["sql-runner"].container_definitions) : c if c.name == "init-volumes"]).essential == false
      && one([for c in jsondecode(aws_ecs_task_definition.job["sql-runner"].container_definitions) : c if c.name == "init-volumes"]).image == var.fargate.images["sql-runner"]
    )
    error_message = "an init container of the same image runs as root, non-essential"
  }

  assert {
    condition = (
      jsonencode(one([for c in jsondecode(aws_ecs_task_definition.job["sql-runner"].container_definitions) : c if c.name == "job"]).dependsOn) == jsonencode([{ containerName = "init-volumes", condition = "SUCCESS" }])
      && one([for c in jsondecode(aws_ecs_task_definition.job["sql-runner"].container_definitions) : c if c.name == "job"]).user == "10001"
    )
    error_message = "the job container runs as 10001 and starts only after the init container succeeded"
  }

  assert {
    condition = (
      jsonencode(one([for c in jsondecode(aws_ecs_task_definition.job["sql-runner"].container_definitions) : c if c.name == "init-volumes"]).mountPoints)
      == jsonencode(one([for c in jsondecode(aws_ecs_task_definition.job["sql-runner"].container_definitions) : c if c.name == "job"]).mountPoints)
      && strcontains(one([for c in jsondecode(aws_ecs_task_definition.job["sql-runner"].container_definitions) : c if c.name == "init-volumes"]).command[0], "chown 10001:10001 /work /out")
    )
    error_message = "the init container opens the same volumes to uid 10001"
  }

  assert {
    condition     = jsondecode(aws_lambda_function.this["prepare_job"].environment[0].variables.TASK_DEFINITIONS)["sql-runner"] == aws_ecs_task_definition.job["sql-runner"].arn
    error_message = "prepare_job resolves image keys to task definitions"
  }

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.lambda_errors) == 3 && length(aws_cloudwatch_metric_alarm.sfn) == 6 && aws_cloudwatch_metric_alarm.watchdog_silent[0].treat_missing_data == "breaching"
    error_message = "alarms: Lambda errors and a breaching 'watchdog not running'"
  }
}

run "container_insights_off_by_default" {
  command = apply

  assert {
    condition     = one([for s in aws_ecs_cluster.this[0].setting : s.value if s.name == "containerInsights"]) == "disabled"
    error_message = "Container Insights is off by default (its log group outlives destroy)"
  }
}

run "dumps_are_kms_only_and_expire" {
  command = apply

  assert {
    condition     = one([for r in aws_s3_bucket_lifecycle_configuration.artifacts[0].rule : r.expiration[0].days if r.id == "target-dumps"]) == 14
    error_message = "dumps/ expire after dump_retention_days (14)"
  }

  assert {
    condition     = strcontains(aws_s3_bucket_policy.artifacts[0].policy, "DenyDumpsWithoutKms")
    error_message = "dumps/ accept only SSE-KMS"
  }
}

run "network_contract_without_snapshot_dumps" {
  command = plan

  assert {
    condition     = !contains(output.network_contract.required_vpc_endpoints, "rds")
    error_message = "no rds endpoint while no task has snapshot_dumps"
  }
}

# dump.method = snapshot calls the RDS API from the jobs: the rds interface endpoint becomes required.
run "network_contract_with_snapshot_dumps" {
  command = plan

  variables {
    job_secret_arns = ["arn:aws:secretsmanager:eu-west-2:111122223333:secret:dms/ledger/target/runner-XyZ123"]
    projects = {
      ledger = {
        tasks = {
          core = {
            connections = {
              target = {
                engine               = "postgresql"
                secret_arn           = "arn:aws:secretsmanager:eu-west-2:111122223333:secret:dms/ledger/target/runner-XyZ123"
                data_api_cluster_arn = "arn:aws:rds:eu-west-2:111122223333:cluster:ledger-target"
              }
            }
            defaults = { snapshot_dumps = true }
          }
        }
      }
    }
  }

  assert {
    condition     = contains(output.network_contract.required_vpc_endpoints, "rds")
    error_message = "a task with snapshot_dumps makes the rds endpoint required"
  }
}

# Fargate is an optional third runtime (ADR 0011): with the default fargate = {} the module
# creates no ECS cluster, no task definitions, no ECS roles and asks for no ECR endpoints.
run "no_ecs_resources_by_default" {
  command = apply

  variables {
    fargate = {}
  }

  assert {
    condition     = length(aws_ecs_cluster.this) == 0 && length(aws_ecs_task_definition.job) == 0
    error_message = "no ECS cluster and no task definitions without fargate.images"
  }

  assert {
    condition     = length(aws_iam_role.ecs_execution) == 0 && length(aws_iam_role.ecs_task) == 0 && length(aws_iam_role_policy.ecs_execution) == 0 && length(aws_iam_role_policy.ecs_task) == 0
    error_message = "no ECS execution or task roles without fargate.images"
  }

  assert {
    condition     = output.ecs_cluster_arn == null && output.task_definition_arns == {}
    error_message = "ecs_cluster_arn is null without fargate.images"
  }

  assert {
    condition     = !contains(output.network_contract.required_vpc_endpoints, "ecr.api") && !contains(output.network_contract.required_vpc_endpoints, "ecr.dkr")
    error_message = "no ECR endpoints in the network contract without fargate.images"
  }

  assert {
    condition     = !strcontains(aws_iam_role.job["ledger"].assume_role_policy, "ecs-tasks.amazonaws.com") && !strcontains(aws_iam_role_policy.job_runner.policy, "ecs:RunTask") && !strcontains(aws_iam_role_policy.job_runner.policy, "iam:PassRole")
    error_message = "no job role trusts ECS tasks and the job-runner neither runs tasks nor passes roles"
  }

  assert {
    condition     = jsondecode(aws_ssm_parameter.task["ledger/core"].value).runtimes.fargate == false && aws_lambda_function.this["prepare_job"].environment[0].variables.TASK_DEFINITIONS == "{}"
    error_message = "the registration and prepare_job know there is no Fargate runtime"
  }
}

# The SSM runtime is opt-in: nothing of it exists and nothing can reach a runner by default.
run "no_ssm_runtime_by_default" {
  command = apply

  assert {
    condition     = length(aws_ssm_document.orch_job) == 0 && length(aws_s3_object.ssm_runtime) == 0 && length(data.archive_file.ssm_runtime) == 0
    error_message = "no document and no runtime zip without ssm.enabled"
  }

  assert {
    condition     = !strcontains(aws_iam_role_policy.job_runner.policy, "ssm:") && !strcontains(aws_iam_role.job["ledger"].assume_role_policy, "SsmRunnerInstances")
    error_message = "without ssm.enabled the job-runner sends no command and no job role trusts a runner"
  }

  assert {
    condition     = aws_lambda_function.this["prepare_job"].environment[0].variables.SSM_JOBS == "0" && aws_lambda_function.this["resolve_context"].environment[0].variables.SSM_JOBS == "0"
    error_message = "prepare_job refuses runtime ssm and dumps stay off SSM"
  }

  assert {
    condition     = output.ssm_runner_policy_json == null && output.ssm_document_content == null && !contains(output.network_contract.required_vpc_endpoints, "ec2messages")
    error_message = "no SSM outputs and no SSM agent endpoints by default"
  }

  assert {
    condition     = jsondecode(aws_ssm_parameter.task["ledger/core"].value).runtimes == { ssm = false, fargate = true }
    error_message = "the registration tells orch.py restore which runtimes exist"
  }
}
