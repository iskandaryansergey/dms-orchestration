# Contract with the upstream dms module. The caller passes the module's outputs to
# `dms`; these runs use the exact output shapes of one implementation, extra outputs
# included.
# See docs/upstream-modules-contract.md.

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  environment = "dev"
  vpc_id      = "vpc-0123456789abcdef0"
  subnet_ids  = ["subnet-aaaa1111", "subnet-bbbb2222"]
  # Every output of modules/dms in provisioned mode (governance & co. abridged:
  # their content does not matter, only that extra attributes are accepted).
  dms = {
    replication_instance_arn         = "arn:aws:dms:eu-west-2:111122223333:rep:RI1"
    replication_instance_id          = "ledger-mig"
    replication_instance_private_ips = ["10.0.1.10"]
    replication_subnet_group_id      = "ledger-mig"
    serverless_replication_arns      = {}
    endpoint_arns                    = { source = "arn:aws:dms:eu-west-2:111122223333:endpoint:SRC", target = "arn:aws:dms:eu-west-2:111122223333:endpoint:TGT" }
    task_arns                        = { core = "arn:aws:dms:eu-west-2:111122223333:task:CORE1" }
    task_ids                         = { core = "ledger-mig-core" }
    task_resource_ids                = { core = "CORE1" }
    validation_task_arns             = {}
    certificate_arns                 = {}
    endpoint_secret_role_arns        = { source = "arn:aws:iam::111122223333:role/ledger-mig-source-secret" }
    s3_target_role_arns              = {}
    log_group_name                   = "dms-tasks-ledger-mig"
    task_settings_json               = { core = "{}" }
    governance                       = { compute_mode = "provisioned", acknowledged_checks = [] }
    verify_commands                  = { task_status = "aws dms describe-replication-tasks ..." }
  }
  job_secret_arns     = ["arn:aws:secretsmanager:eu-west-2:111122223333:secret:ok-AbCdEf"]
  projects            = { ledger = { tasks = { core = {} } } }
  acknowledged_checks = ["own_sns_topic_outside_sandbox", "state_in_the_dms_workspace", "dumps_without_cmk", "no_cmk", "include_execution_data", "lambda_reserved_concurrency"]
}

run "upstream_provisioned_outputs_as_is" {
  command = plan

  assert {
    condition     = jsondecode(aws_ssm_parameter.task["ledger/core"].value).taskResourceId == "CORE1"
    error_message = "task_resource_ids from the upstream module must reach the task registration"
  }

  assert {
    condition     = jsondecode(aws_ssm_parameter.task["ledger/core"].value).logGroupName == "dms-tasks-ledger-mig"
    error_message = "log_group_name from the upstream module must reach the task registration"
  }
}

# An older version of the module has no task_resource_ids output: still accepted.
run "upstream_dms_module_branch_without_task_resource_ids" {
  command = plan
  variables {
    dms = {
      replication_instance_arn = "arn:aws:dms:eu-west-2:111122223333:rep:RI1"
      replication_instance_id  = "ledger-mig"
      endpoint_arns            = {}
      task_arns                = { core = "arn:aws:dms:eu-west-2:111122223333:task:CORE1" }
      task_ids                 = { core = "ledger-mig-core" }
      log_group_name           = "dms-tasks-ledger-mig"
    }
  }
}

# compute_mode = "serverless": instance outputs are null, task_arns is empty and the
# work is in serverless_replication_arns. The orchestrator drives replication tasks
# only, so this must fail at plan with a message that names the reason.
run "upstream_serverless_mode_refused" {
  command = plan
  variables {
    dms = {
      replication_instance_arn    = null
      replication_instance_id     = null
      serverless_replication_arns = { core = "arn:aws:dms:eu-west-2:111122223333:replication-config:CFG1" }
      endpoint_arns               = {}
      task_arns                   = {}
      task_resource_ids           = {}
      log_group_name              = null
    }
  }
  expect_failures = [var.dms]
}

# Provisioned mode but create = false (or a broken wiring): null instance outputs
# with tasks present must not slip through as "null" in the registration.
run "null_replication_instance_refused" {
  command = plan
  variables {
    dms = {
      replication_instance_arn = null
      replication_instance_id  = null
      task_arns                = { core = "arn:aws:dms:eu-west-2:111122223333:task:CORE1" }
    }
  }
  expect_failures = [var.dms]
}
