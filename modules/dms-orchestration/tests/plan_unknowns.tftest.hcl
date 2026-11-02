# Plan-time unknowns: inputs that come from resources created in the same plan.

mock_provider "aws" {
  source = "./tests/mocks"
}

run "database_sg_created_in_the_same_plan" {
  command = plan

  module {
    source = "./tests/fixtures/new_db_sg"
  }

  assert {
    condition     = length(module.orch.state_machine_arns) == 3
    error_message = "the module plans when database security group ids are not known yet"
  }
}

# One runner per project from a runner module created in the same plan (e.g. dms-sql-bastion):
# the runner takes its project's policy from the module, the module its instance and role.
run "runner_per_project_from_a_runner_module" {
  command = plan

  module {
    source = "./tests/fixtures/runner_per_project"
  }

  assert {
    condition     = toset(keys(module.orch.ssm_runner_policy_json)) == toset(["ledger", "cards"])
    error_message = "each project's runner gets its own policy without a dependency cycle"
  }
}
