# One runner per project, wired like the README: the runner takes its project's policy from
# the module, the module takes the runner's instance and role. No dependency cycle.
terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

locals {
  projects = ["ledger", "cards"]
}

module "orch" {
  source = "../../.."

  environment = "dev"
  vpc_id      = "vpc-0123456789abcdef0"
  subnet_ids  = ["subnet-aaaa1111", "subnet-bbbb2222"]
  dms = {
    replication_instance_arn = "arn:aws:dms:eu-west-2:111122223333:rep:RI1"
    replication_instance_id  = "dms-dev-ri"
    task_arns = {
      core  = "arn:aws:dms:eu-west-2:111122223333:task:CORE1"
      extra = "arn:aws:dms:eu-west-2:111122223333:task:EXTRA1"
    }
  }
  projects = {
    ledger = { tasks = { core = {} } }
    cards  = { tasks = { extra = {} } }
  }
  ssm = {
    enabled = true
    runners = {
      for p in local.projects : p => { instance_ids = [module.runner[p].instance_id], instance_role_arn = module.runner[p].role_arn }
    }
  }
  acknowledged_checks = ["own_sns_topic_outside_sandbox", "state_in_the_dms_workspace", "dumps_without_cmk", "no_cmk", "lambda_reserved_concurrency"]
}

module "runner" {
  source   = "./runner"
  for_each = toset(local.projects)

  name                       = "mig-${each.key}"
  additional_iam_policy_json = module.orch.ssm_runner_policy_json[each.key]
}
