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
