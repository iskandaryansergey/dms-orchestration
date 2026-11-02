# Shared module conventions: a create switch, non-nullable inputs, "" for unset optional
# strings, ManagedBy/Module tags, the KMS key policy contract v2 next to the v1 statements.

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  environment = "dev"
  vpc_id      = "vpc-0123456789abcdef0"
  subnet_ids  = ["subnet-aaaa1111", "subnet-bbbb2222"]
  dms = {
    replication_instance_arn = "arn:aws:dms:eu-west-2:111122223333:rep:RI1"
    replication_instance_id  = "dms-dev-ri"
    task_arns                = { core = "arn:aws:dms:eu-west-2:111122223333:task:CORE1" }
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
          jobs = { allow_unpinned = true }
        }
      }
    }
  }
  acknowledged_checks = ["own_sns_topic_outside_sandbox", "state_in_the_dms_workspace", "dumps_without_cmk", "no_cmk", "include_execution_data", "lambda_reserved_concurrency"]
}

# create = false with every optional part switched on: nothing is created, nothing is
# packaged, and the outputs are empty.
run "create_false_creates_nothing" {
  command = apply

  variables {
    create                   = false
    kms_key_arn              = "arn:aws:kms:eu-west-2:111122223333:key/11111111-2222-3333-4444-555555555555"
    permissions_boundary_arn = "arn:aws:iam::111122223333:policy/boundary"
    fargate                  = { images = { sql-runner = "111122223333.dkr.ecr.eu-west-2.amazonaws.com/dms-orch/sql-runner:0.1.0" } }
    ssm = {
      enabled           = true
      instance_ids      = ["i-0123456789abcdef0"]
      instance_role_arn = "arn:aws:iam::111122223333:role/team-dms-runner"
    }
    assessment = {
      role_arn = "arn:aws:iam::111122223333:role/dms-assessment"
      bucket   = "dms-assessment-results"
    }
    database_cidr_blocks        = ["10.0.16.0/20"]
    database_security_group_ids = ["sg-0123456789abcdef0"]
  }

  assert {
    condition = alltrue([for n in [
      length(aws_cloudwatch_metric_alarm.sfn), length(aws_cloudwatch_metric_alarm.lambda_errors),
      length(aws_cloudwatch_metric_alarm.dlq), length(aws_cloudwatch_metric_alarm.watchdog_silent),
      length(aws_cloudwatch_metric_alarm.orphans),
      length(aws_cloudwatch_event_rule.dms_task_state), length(aws_cloudwatch_event_target.dms_task_state),
      length(aws_cloudwatch_event_rule.execution_status), length(aws_cloudwatch_event_target.execution_status),
      length(aws_sqs_queue.dlq), length(aws_sqs_queue_policy.dlq), length(aws_scheduler_schedule.watchdog),
      length(aws_iam_role.migrate), length(aws_iam_role_policy.migrate),
      length(aws_iam_role.post), length(aws_iam_role_policy.post),
      length(aws_iam_role.job_runner), length(aws_iam_role_policy.job_runner),
      length(aws_iam_role.lambda_core), length(aws_iam_role_policy.lambda_core),
      length(aws_iam_role.lambda_waiter), length(aws_iam_role_policy.lambda_waiter),
      length(aws_iam_role.lambda_jobs), length(aws_iam_role_policy.lambda_jobs),
      length(aws_iam_role.ecs_execution), length(aws_iam_role_policy.ecs_execution),
      length(aws_iam_role.ecs_task), length(aws_iam_role_policy.ecs_task),
      length(aws_iam_role.scheduler), length(aws_iam_role_policy.scheduler),
      length(aws_iam_role.job), length(aws_iam_role_policy_attachment.job_iam_database), length(aws_iam_role_policy.job),
      length(data.archive_file.lambda), length(aws_cloudwatch_log_group.lambda), length(aws_lambda_function.this),
      length(aws_lambda_function_event_invoke_config.dms_event_handler), length(aws_lambda_function_event_invoke_config.release_lock),
      length(aws_lambda_permission.dms_events), length(aws_lambda_permission.sfn_status),
      length(aws_security_group.jobs), length(data.aws_vpc.jobs), length(data.aws_prefix_list.gateway),
      length(aws_vpc_security_group_egress_rule.https_cidr), length(aws_vpc_security_group_egress_rule.https_prefix_list),
      length(aws_vpc_security_group_egress_rule.db_cidr), length(aws_vpc_security_group_egress_rule.db_sg),
      length(data.archive_file.ssm_runtime), length(aws_s3_object.ssm_runtime), length(aws_ssm_document.orch_job),
      length(aws_cloudwatch_log_group.sfn), length(aws_sfn_state_machine.job_runner), length(aws_sfn_state_machine.run),
      length(aws_ecs_cluster.this), length(aws_cloudwatch_log_group.jobs), length(aws_ecs_task_definition.job),
      length(aws_dynamodb_table.this), length(aws_s3_bucket.artifacts), length(aws_s3_bucket_ownership_controls.artifacts),
      length(aws_s3_bucket_public_access_block.artifacts), length(aws_s3_bucket_versioning.artifacts),
      length(aws_s3_bucket_server_side_encryption_configuration.artifacts), length(aws_s3_bucket_lifecycle_configuration.artifacts),
      length(aws_s3_bucket_policy.artifacts), length(aws_sns_topic.this), length(aws_sns_topic_policy.this),
      length(aws_ssm_parameter.task),
    ] : n == 0])
    error_message = "create = false must not create (or package) anything"
  }

  assert {
    condition = alltrue([
      length(output.state_machine_arns) == 0, length(output.lambda_function_arns) == 0, length(output.task_definition_arns) == 0,
      length(output.ssm_parameter_names) == 0, length(output.jobset_publisher_policy_json) == 0,
      output.table_name == null, output.table_arn == null, output.artifacts_bucket == null, output.sns_topic_arn == null,
      output.jobs_security_group_id == null, output.ecs_cluster_arn == null, output.operator_policy_json == null,
      output.ssm_runner_policy_json == null, output.ssm_document_content == null, output.ssm_document_name == null,
      output.kms_key_policy_requirements == "[]", output.kms_key_policy_statements == "[]",
      output.bucket_requirements == null, output.network_contract == null,
      output.run_request_example == null, length(output.verify_commands) == 0,
    ])
    error_message = "create = false leaves every output empty"
  }
}

# Unset optional strings are "" (never null), the switches have defaults, and null for a
# non-nullable input means its default.
run "unset_optional_strings_are_empty" {
  command = apply

  variables {
    kms_key_arn              = null
    permissions_boundary_arn = null
    assessment               = null
  }

  assert {
    condition = alltrue([
      var.create, var.kms_key_arn == "", var.permissions_boundary_arn == "", var.artifacts_bucket_name == "",
      var.fargate.cluster_arn == "", var.dms.log_group_name == "", var.assessment.role_arn == "",
      var.ssm.instance_role_arn == "", var.ssm.document_name_override == "",
      var.lambda.artifact_s3_bucket == "", var.lambda.reserved_concurrency == -1,
      var.https_egress_vpc_cidr, var.https_egress_gateway_endpoints,
      length(var.https_egress_cidr_blocks) == 0, length(var.https_egress_prefix_list_ids) == 0,
      var.projects["ledger"].tasks["core"].task_arn_key == "",
      var.projects["ledger"].tasks["core"].connections["source"].data_api_cluster_arn == "",
    ])
    error_message = "unset optional strings are \"\", switches are booleans, reserved_concurrency -1"
  }

  # The registration JSON keeps null for unset values: the Python side tests truthiness.
  assert {
    condition = alltrue([
      jsondecode(aws_ssm_parameter.task["ledger/core"].value).logGroupName == null,
      jsondecode(aws_ssm_parameter.task["ledger/core"].value).assessment == null,
      jsondecode(aws_ssm_parameter.task["ledger/core"].value).jobs.defaultVersion == null,
      jsondecode(aws_ssm_parameter.task["ledger/core"].value).connections.source.dataApiClusterArn == null,
      jsondecode(aws_ssm_parameter.task["ledger/core"].value).connections.source.iamUser == null,
      jsondecode(aws_ssm_parameter.task["ledger/core"].value).connections.source.host == "source.cluster-xyz.eu-west-2.rds.amazonaws.com",
    ])
    error_message = "\"\" becomes null in the task registration"
  }

  assert {
    condition = alltrue([
      aws_iam_role.migrate[0].permissions_boundary == null,
      aws_sqs_queue.dlq[0].kms_master_key_id == null,
      aws_cloudwatch_log_group.jobs[0].kms_key_id == null,
      aws_lambda_function.this["watchdog"].reserved_concurrent_executions == -1,
      aws_lambda_function.this["watchdog"].kms_key_arn == null,
      keys(aws_vpc_security_group_egress_rule.https_cidr) == ["10.20.0.0/16"],
      length(aws_vpc_security_group_egress_rule.https_prefix_list) == 2,
    ])
    error_message = "\"\" reaches no resource argument: AWS managed keys, no boundary, unreserved concurrency, VPC CIDR and gateway prefix lists"
  }
}

run "egress_switches_off_keep_only_the_given_ranges" {
  command = apply

  variables {
    https_egress_vpc_cidr          = false
    https_egress_gateway_endpoints = false
    https_egress_cidr_blocks       = ["10.1.2.0/24"]
  }

  assert {
    condition     = keys(aws_vpc_security_group_egress_rule.https_cidr) == ["10.1.2.0/24"] && length(aws_vpc_security_group_egress_rule.https_prefix_list) == 0 && length(data.aws_vpc.jobs) == 0
    error_message = "with both switches off only the given CIDRs remain"
  }
}

run "managed_by_and_module_tags" {
  command = apply

  variables {
    tags = { CostCentre = "dms" }
  }

  assert {
    condition = alltrue([for t in [aws_dynamodb_table.this[0].tags, aws_iam_role.migrate[0].tags, aws_sfn_state_machine.job_runner[0].tags, aws_iam_role.job["ledger"].tags] :
      t["ManagedBy"] == "terraform" && t["Module"] == "dms-orchestration" && t["dms-orch:module"] == "dms-orchestration" && t["CostCentre"] == "dms"
    ])
    error_message = "every resource carries ManagedBy = terraform and Module = dms-orchestration besides the dms-orch:* tags"
  }
}

# KMS key policy contract v2: one item per need, principals by kind, symbolic roles the key
# module resolves (deployer = the identity that applies this module, module = the module's
# roles listed in arns). kms_key_policy_statements keeps the v1 statement list.
run "kms_contract_v2_shape" {
  command = apply

  variables {
    kms_key_arn = "arn:aws:kms:eu-west-2:111122223333:key/11111111-2222-3333-4444-555555555555"
  }

  assert {
    condition = alltrue([for item in jsondecode(output.kms_key_policy_requirements) :
      toset(keys(item)) == toset(["sid", "principals", "actions", "conditions", "purpose"])
      && toset(keys(item.principals)) == toset(["services", "arns", "roles"])
      && length(item.actions) > 0 && item.purpose != ""
      && alltrue([for r in item.principals.roles : contains(["deployer", "module"], r)])
    ])
    error_message = "every v2 item has sid, principals {services, arns, roles}, actions, conditions and purpose"
  }

  assert {
    condition = alltrue([
      contains(one([for i in jsondecode(output.kms_key_policy_requirements) : i.principals.arns if i.sid == "ModuleRoles"]), "arn:aws:iam::111122223333:role/dms-orch-dev-job-ledger"),
      one([for i in jsondecode(output.kms_key_policy_requirements) : i.principals.roles if i.sid == "ModuleRoles"]) == ["module"],
      contains(one([for i in jsondecode(output.kms_key_policy_requirements) : i.principals.services if i.sid == "EventsToEncryptedQueueAndScheduler"]), "cloudwatch.amazonaws.com"),
      one([for i in jsondecode(output.kms_key_policy_requirements) : i.conditions if i.sid == "StepFunctionsAndLogs"]).StringEquals["aws:SourceAccount"] == "111122223333",
      one([for i in jsondecode(output.kms_key_policy_requirements) : i.principals.roles if i.sid == "Deployer"]) == ["deployer"],
    ])
    error_message = "v2 carries the module role ARNs, the service principals, the source account condition and the deployer"
  }

  assert {
    condition = alltrue([
      [for st in jsondecode(output.kms_key_policy_statements) : st.Sid] == [for i in jsondecode(output.kms_key_policy_requirements) : i.sid if length(i.principals.roles) == 0 || length(i.principals.arns) > 0],
      contains(one([for st in jsondecode(output.kms_key_policy_statements) : st.Principal.AWS if st.Sid == "ModuleRoles"]), "arn:aws:iam::111122223333:role/dms-orch-dev-job-ledger"),
      contains(one([for st in jsondecode(output.kms_key_policy_statements) : st.Principal.Service if st.Sid == "EventsToEncryptedQueueAndScheduler"]), "cloudwatch.amazonaws.com"),
      one([for st in jsondecode(output.kms_key_policy_statements) : st.Condition if st.Sid == "StepFunctionsAndLogs"]).StringEquals["aws:SourceAccount"] == "111122223333",
    ])
    error_message = "kms_key_policy_statements is the v1 list (Sid, Principal, Action, Condition) built from the same items"
  }
}

run "kms_contract_empty_without_a_cmk" {
  command = plan

  assert {
    condition     = output.kms_key_policy_requirements == "[]" && output.kms_key_policy_statements == "[]"
    error_message = "without kms_key_arn there are no key policy requirements"
  }
}
