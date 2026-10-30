# Input validation: bad inputs fail at plan time with a clear message.

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
  job_secret_arns     = ["arn:aws:secretsmanager:eu-west-2:111122223333:secret:ok-AbCdEf"]
  projects            = { ledger = { tasks = { core = {} } } }
  acknowledged_checks = ["own_sns_topic_outside_sandbox", "state_in_the_dms_workspace", "dumps_without_cmk", "no_cmk", "include_execution_data", "lambda_reserved_concurrency"]
}

run "valid_minimal_input_plans" {
  command = plan
}

run "unknown_task_arn_key" {
  command = plan
  variables {
    projects = { ledger = { tasks = { core = { task_arn_key = "missing" } } } }
  }
  expect_failures = [var.projects]
}

run "task_key_not_in_dms_outputs" {
  command = plan
  variables {
    projects = { ledger = { tasks = { other = {} } } }
  }
  expect_failures = [var.projects]
}

run "secret_outside_allow_list" {
  command = plan
  variables {
    projects = { ledger = { tasks = { core = { connections = { source = { engine = "postgresql", secret_arn = "arn:aws:secretsmanager:eu-west-2:111122223333:secret:other-AbCdEf" } } } } } }
  }
  expect_failures = [var.projects]
}

run "bad_reset_policy" {
  command = plan
  variables {
    projects = { ledger = { tasks = { core = { defaults = { target_reset_policy = "sometimes" } } } } }
  }
  expect_failures = [var.projects]
}

run "bad_project_name" {
  command = plan
  variables {
    projects = { Ledger_Prod = { tasks = { core = {} } } }
  }
  expect_failures = [var.projects]
}

run "one_subnet" {
  command = plan
  variables {
    subnet_ids = ["subnet-aaaa1111"]
  }
  expect_failures = [var.subnet_ids]
}


run "bad_kms_arn" {
  command = plan
  variables {
    kms_key_arn = "alias/my-key"
  }
  expect_failures = [var.kms_key_arn]
}

run "bad_environment" {
  command = plan
  variables {
    environment = "production"
  }
  expect_failures = [var.environment]
}

run "bad_task_arn" {
  command = plan
  variables {
    dms = {
      replication_instance_arn = "arn:aws:dms:eu-west-2:111122223333:rep:RI1"
      replication_instance_id  = "dms-dev-ri"
      task_arns                = { core = "not-an-arn" }
    }
  }
  expect_failures = [var.dms]
}

run "https_egress_to_the_internet" {
  command = plan
  variables {
    https_egress_cidr_blocks = ["0.0.0.0/0"]
  }
  expect_failures = [var.https_egress_cidr_blocks]
}

run "https_egress_explicit_ranges" {
  command = plan
  variables {
    https_egress_vpc_cidr          = false
    https_egress_gateway_endpoints = false
    https_egress_cidr_blocks       = ["10.1.2.0/24"]
    https_egress_prefix_list_ids   = ["pl-0123456789abcdef0"]
  }
  assert {
    condition     = keys(aws_vpc_security_group_egress_rule.https_cidr) == ["10.1.2.0/24"] && keys(aws_vpc_security_group_egress_rule.https_prefix_list) == ["0"]
    error_message = "with the switches off only the explicit https egress ranges remain"
  }
}

run "completion_full_load_complete_is_out_of_contract" {
  command = plan
  variables {
    projects = { ledger = { tasks = { core = { defaults = { completion = "full-load-complete" } } } } }
  }
  expect_failures = [var.projects]
}

# connections[*].auth = "iam" (token login for Fargate jobs).
run "iam_connection_without_a_policy_is_refused" {
  command = plan
  variables {
    projects = { ledger = { tasks = { core = { connections = { target_iam = { engine = "postgresql", auth = "iam", iam_user = "orch_iam", host = "t.cluster.local" } } } } } }
  }
  expect_failures = [var.projects]
}

run "iam_connection_without_a_user_or_host_is_refused" {
  command = plan
  variables {
    projects = { ledger = { tasks = { core = { connections = { target_iam = { engine = "postgresql", auth = "iam", iam_policy_arn = "arn:aws:iam::111122223333:policy/dbuser" } } } } } }
  }
  expect_failures = [var.projects]
}

run "secret_connection_without_a_secret_is_refused" {
  command = plan
  variables {
    projects = { ledger = { tasks = { core = { connections = { target = { engine = "postgresql" } } } } } }
  }
  expect_failures = [var.projects]
}

run "unknown_auth_is_refused" {
  command = plan
  variables {
    projects = { ledger = { tasks = { core = { connections = { target = { engine = "postgresql", auth = "kerberos", secret_arn = "arn:aws:secretsmanager:eu-west-2:111122223333:secret:ok-AbCdEf" } } } } } }
  }
  expect_failures = [var.projects]
}

# Names built from a project key (snapshot identifiers, prefixes) must not reach into another
# project's: no project key may be a prefix of another.
run "project_key_prefix_of_another_is_refused" {
  command = plan
  variables {
    dms = {
      replication_instance_arn = "arn:aws:dms:eu-west-2:111122223333:rep:RI1"
      replication_instance_id  = "dms-dev-ri"
      task_arns                = { core = "arn:aws:dms:eu-west-2:111122223333:task:CORE1", extra = "arn:aws:dms:eu-west-2:111122223333:task:EXTRA1" }
    }
    projects = {
      ledger      = { tasks = { core = {} } }
      ledger-prod = { tasks = { extra = {} } }
    }
  }
  expect_failures = [var.projects]
}

# Job sets are pinned by the bundle hash (jobset_tool.py hash): a malformed hash, or a default
# version without one, fails at plan time unless the task allows unpinned job sets (sandbox).
run "job_set_bundle_hash_must_be_sha256" {
  command = plan
  variables {
    projects = { ledger = { tasks = { core = { jobs = { bundles = { v1 = "not-a-hash" } } } } } }
  }
  expect_failures = [var.projects]
}

run "job_set_bundle_version_must_be_a_version_name" {
  command = plan
  variables {
    projects = { ledger = { tasks = { core = { jobs = { bundles = { "v 1/x" = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0" } } } } } }
  }
  expect_failures = [var.projects]
}

run "default_job_set_version_without_a_hash_is_refused" {
  command = plan
  variables {
    projects = { ledger = { tasks = { core = { jobs = { default_version = "v1" } } } } }
  }
  expect_failures = [var.projects]
}

run "default_job_set_version_pinned_or_unpinned_allowed_plans" {
  command = plan
  variables {
    dms = {
      replication_instance_arn = "arn:aws:dms:eu-west-2:111122223333:rep:RI1"
      replication_instance_id  = "dms-dev-ri"
      task_arns                = { core = "arn:aws:dms:eu-west-2:111122223333:task:CORE1", extra = "arn:aws:dms:eu-west-2:111122223333:task:EXTRA1" }
    }
    projects = { ledger = { tasks = {
      core  = { jobs = { default_version = "v1", bundles = { v1 = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0" } } }
      extra = { jobs = { default_version = "e2e", allow_unpinned = true } }
    } } }
  }
}

run "project_keys_that_are_not_prefixes_plan" {
  command = plan
  variables {
    dms = {
      replication_instance_arn = "arn:aws:dms:eu-west-2:111122223333:rep:RI1"
      replication_instance_id  = "dms-dev-ri"
      task_arns                = { core = "arn:aws:dms:eu-west-2:111122223333:task:CORE1", extra = "arn:aws:dms:eu-west-2:111122223333:task:EXTRA1" }
    }
    projects = {
      ledger = { tasks = { core = {} } }
      cards  = { tasks = { extra = {} } }
    }
  }
}
