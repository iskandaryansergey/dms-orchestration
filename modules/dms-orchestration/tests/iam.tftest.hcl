# Least-privilege checks on every policy the module builds (policies are jsonencode'd locals).

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
    task_arns = {
      core  = "arn:aws:dms:eu-west-2:111122223333:task:CORE1"
      extra = "arn:aws:dms:eu-west-2:111122223333:task:EXTRA1"
    }
    log_group_name = "dms-tasks-dms-dev-ri"
  }
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
        }
      }
    }
  }
  fargate = {
    images = { sql-runner = "111122223333.dkr.ecr.eu-west-2.amazonaws.com/dms-orch/sql-runner:0.1.0" }
  }
  acknowledged_checks = ["own_sns_topic_outside_sandbox", "state_in_the_dms_workspace", "dumps_without_cmk", "no_cmk", "include_execution_data", "lambda_reserved_concurrency"]
}

run "least_privilege" {
  command = apply

  # Actions allowed on Resource "*" because the service has no resource-level permissions.
  variables {
    permissions_boundary_arn = "arn:aws:iam::111122223333:policy/boundary"
  }

  assert {
    condition = alltrue(flatten([
      for p in concat([aws_iam_role_policy.migrate, aws_iam_role_policy.post, aws_iam_role_policy.job_runner, aws_iam_role_policy.lambda_core, aws_iam_role_policy.lambda_waiter, aws_iam_role_policy.lambda_jobs, aws_iam_role_policy.ecs_execution, aws_iam_role_policy.ecs_task, aws_iam_role_policy.scheduler], values(aws_iam_role_policy.job)) : [
        for st in jsondecode(p.policy).Statement : alltrue([for a in flatten([st.Action]) : a != "*" && !endswith(a, ":*")])
      ]
    ]))
    error_message = "no policy may grant '*' or '<service>:*'"
  }

  assert {
    condition = alltrue(flatten([
      for p in concat([aws_iam_role_policy.migrate, aws_iam_role_policy.post, aws_iam_role_policy.job_runner, aws_iam_role_policy.lambda_core, aws_iam_role_policy.lambda_waiter, aws_iam_role_policy.lambda_jobs, aws_iam_role_policy.ecs_execution, aws_iam_role_policy.ecs_task, aws_iam_role_policy.scheduler], values(aws_iam_role_policy.job)) : [
        for st in jsondecode(p.policy).Statement : alltrue([
          for a in flatten([st.Action]) : contains([
            "dms:DescribeReplicationTasks", "dms:DescribeTableStatistics", "dms:DescribeConnections", "dms:DescribeEvents", "dms:DescribeEndpoints", "dms:DescribeReplicationTaskAssessmentRuns",
            "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses", "ec2:DescribeSubnets", "ec2:DescribeVpcs",
            "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
            "ecr:GetAuthorizationToken", "ecs:StopTask", "ecs:DescribeTasks",
            "xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets",
          ], a)
        ]) if st.Resource == "*"
      ]
    ]))
    error_message = "Resource '*' is only allowed for actions without resource-level permissions"
  }

  assert {
    condition     = toset(one([for st in jsondecode(aws_iam_role_policy.migrate.policy).Statement : st.Resource if st.Sid == "StartDmsTasks"])) == toset(values(var.dms.task_arns))
    error_message = "dms:StartReplicationTask must be limited to the DMS task ARNs"
  }

  assert {
    condition = alltrue([
      for p in [aws_iam_role_policy.post, aws_iam_role_policy.job_runner, aws_iam_role_policy.lambda_core, aws_iam_role_policy.lambda_waiter, aws_iam_role_policy.lambda_jobs, aws_iam_role_policy.ecs_execution, aws_iam_role_policy.ecs_task] :
      length([for st in jsondecode(p.policy).Statement : st if contains(flatten([st.Action]), "dms:StartReplicationTask")]) == 0
    ])
    error_message = "only migrate starts DMS tasks (ADR 0005)"
  }

  assert {
    condition     = one([for st in jsondecode(aws_iam_role_policy.lambda_waiter.policy).Statement : st.Resource if st.Sid == "StartPost"]) == ["arn:aws:states:eu-west-2:111122223333:stateMachine:${aws_sfn_state_machine.run["post"].name}"]
    error_message = "the handoff Lambdas may start only the post machine"
  }

  assert {
    condition     = length([for p in [aws_iam_role_policy.migrate, aws_iam_role_policy.post, aws_iam_role_policy.lambda_waiter] : p if strcontains(p.policy, "states:SendTask")]) == 0
    error_message = "no task tokens any more (ADR 0005)"
  }

  assert {
    condition     = toset(one([for st in jsondecode(aws_iam_role_policy.job["ledger"].policy).Statement : st.Resource if st.Sid == "ReadProjectSecrets"])) == toset(var.job_secret_arns)
    error_message = "secretsmanager:GetSecretValue only on the project's secrets (all in job_secret_arns)"
  }

  assert {
    condition = alltrue([
      for p in [aws_iam_role_policy.migrate, aws_iam_role_policy.post, aws_iam_role_policy.lambda_core, aws_iam_role_policy.lambda_waiter, aws_iam_role_policy.ecs_execution] :
      length([for st in jsondecode(p.policy).Statement : st if contains(flatten([st.Action]), "secretsmanager:GetSecretValue")]) == 0
    ])
    error_message = "orchestration roles must not read secrets"
  }

  assert {
    condition     = one([for st in jsondecode(aws_iam_role_policy.job_runner.policy).Statement : st.Condition if st.Sid == "PassJobRoles"]).StringEquals["iam:PassedToService"] == "ecs-tasks.amazonaws.com"
    error_message = "iam:PassRole only to ECS tasks"
  }

  assert {
    condition     = one([for st in jsondecode(aws_iam_role_policy.job["ledger"].policy).Statement : st.Resource if st.Sid == "DataApi"]) == ["arn:aws:rds:eu-west-2:111122223333:cluster:ledger-target"]
    error_message = "Data API only on the registered clusters"
  }

  assert {
    condition     = alltrue([for r in [aws_iam_role.migrate, aws_iam_role.post, aws_iam_role.lambda_core, aws_iam_role.ecs_task, aws_iam_role.scheduler] : r.permissions_boundary == var.permissions_boundary_arn])
    error_message = "every role gets the permissions boundary"
  }

  assert {
    condition     = jsondecode(aws_iam_role.post.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "111122223333"
    error_message = "trust policies are scoped to the account (confused deputy)"
  }

  assert {
    condition     = one([for st in jsondecode(aws_iam_role_policy.lambda_jobs.policy).Statement : st.Condition if st.Sid == "RecordRestoreBaseline"])["ForAllValues:StringLike"]["dynamodb:LeadingKeys"] == ["TARGET#*"]
    error_message = "built-in jobs write only TARGET# items (the restore baseline)"
  }

  assert {
    condition     = length([for st in jsondecode(aws_iam_role_policy.lambda_jobs.policy).Statement : st if contains(flatten([st.Action]), "dynamodb:PutItem")]) == 1
    error_message = "built-in jobs have exactly one DynamoDB write statement"
  }

  assert {
    condition     = length([for st in jsondecode(output.operator_policy_json).Statement : st if contains(flatten([st.Action]), "states:StartExecution")]) == 2
    error_message = "operator policy can start migrate (and post for a repeat)"
  }
}

run "no_secrets_no_data_api_no_fargate" {
  command = apply

  variables {
    job_secret_arns     = []
    fargate             = {}
    acknowledged_checks = ["own_sns_topic_outside_sandbox", "state_in_the_dms_workspace", "dumps_without_cmk", "no_cmk", "include_execution_data", "lambda_reserved_concurrency", "no_fargate_images"]
    projects            = { ledger = { tasks = { core = {} } } }
  }

  assert {
    condition     = length([for st in jsondecode(aws_iam_role_policy.job["ledger"].policy).Statement : st if contains(["ReadProjectSecrets", "DataApi"], st.Sid)]) == 0
    error_message = "no empty secret / Data API statements"
  }

  assert {
    condition     = length([for st in jsondecode(aws_iam_role_policy.job_runner.policy).Statement : st if st.Sid == "RunJobTasks"]) == 0
    error_message = "no ECS permissions without Fargate images"
  }

  # Found in the sandbox: CreateStateMachine checks the managed rule of every .sync
  # integration in the definition, and job-runner always has RunFargateJob.
  assert {
    condition     = length([for st in jsondecode(aws_iam_role_policy.job_runner.policy).Statement : st if st.Sid == "SyncIntegrationRule"]) == 1
    error_message = "job-runner needs the ECS .sync managed rule even without Fargate images"
  }

  assert {
    condition     = length(aws_ecs_task_definition.job) == 0
    error_message = "no task definitions without images"
  }
}

# Review finding 4: a job of one project must not read another project's secrets.
run "per_project_job_roles" {
  command = apply

  variables {
    job_secret_arns = [
      "arn:aws:secretsmanager:eu-west-2:111122223333:secret:dms/ledger/target/runner-XyZ123",
      "arn:aws:secretsmanager:eu-west-2:111122223333:secret:dms/cards/target/runner-AbC456",
    ]
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
          }
        }
      }
      cards = {
        tasks = {
          extra = {
            connections = {
              target = {
                engine     = "postgresql"
                secret_arn = "arn:aws:secretsmanager:eu-west-2:111122223333:secret:dms/cards/target/runner-AbC456"
              }
            }
          }
        }
      }
    }
  }

  assert {
    condition     = toset(keys(aws_iam_role.job)) == toset(["ledger", "cards"])
    error_message = "one job role per project"
  }

  assert {
    condition = (
      one([for st in jsondecode(aws_iam_role_policy.job["ledger"].policy).Statement : st.Resource if st.Sid == "ReadProjectSecrets"]) == ["arn:aws:secretsmanager:eu-west-2:111122223333:secret:dms/ledger/target/runner-XyZ123"]
      && one([for st in jsondecode(aws_iam_role_policy.job["cards"].policy).Statement : st.Resource if st.Sid == "ReadProjectSecrets"]) == ["arn:aws:secretsmanager:eu-west-2:111122223333:secret:dms/cards/target/runner-AbC456"]
    )
    error_message = "a project's job role reads only that project's secrets"
  }

  assert {
    condition = (
      one([for st in jsondecode(aws_iam_role_policy.job["ledger"].policy).Statement : st.Resource if st.Sid == "DataApi"]) == ["arn:aws:rds:eu-west-2:111122223333:cluster:ledger-target"]
      && length([for st in jsondecode(aws_iam_role_policy.job["cards"].policy).Statement : st if st.Sid == "DataApi"]) == 0
    )
    error_message = "Data API only on the project's own clusters"
  }

  assert {
    condition     = one([for st in jsondecode(aws_iam_role_policy.job["cards"].policy).Statement : st.Resource if st.Sid == "ReadJobBundles"]) == ["arn:aws:s3:::dms-orch-dev-artifacts-111122223333/jobs/cards/*"]
    error_message = "a project's job role reads only its own job sets"
  }

  assert {
    condition = alltrue([
      for p in [aws_iam_role_policy.lambda_jobs, aws_iam_role_policy.ecs_task] :
      length([for st in jsondecode(p.policy).Statement : st if length(setintersection(toset(flatten([st.Action])), toset(["secretsmanager:GetSecretValue", "rds-data:ExecuteStatement"]))) > 0]) == 0
    ])
    error_message = "shared job roles must not read secrets or use the Data API"
  }

  assert {
    condition     = length([for st in jsondecode(aws_iam_role_policy.lambda_jobs.policy).Statement : st if st.Sid == "TestEndpointConnections"]) == 0
    error_message = "no TestConnection grant without registered endpoint ARNs"
  }

  assert {
    condition     = toset(one([for st in jsondecode(aws_iam_role_policy.lambda_jobs.policy).Statement : st.Resource if st.Sid == "AssumeProjectJobRoles"])) == toset(["arn:aws:iam::111122223333:role/dms-orch-dev-job-ledger", "arn:aws:iam::111122223333:role/dms-orch-dev-job-cards"])
    error_message = "built-in Lambda jobs work through the project's job role"
  }

  assert {
    condition     = contains(one([for st in jsondecode(aws_iam_role_policy.job_runner.policy).Statement : st.Resource if st.Sid == "PassJobRoles"]), "arn:aws:iam::111122223333:role/dms-orch-dev-job-cards")
    error_message = "the job runner may pass project job roles to ECS"
  }

  assert {
    condition     = jsondecode(aws_sfn_state_machine.job_runner.definition).States.RunFargateJob.Arguments.Overrides.TaskRoleArn == "{% $spec.jobRoleArn %}"
    error_message = "Fargate jobs run with the project's job role"
  }

  assert {
    condition = alltrue([
      for st in jsondecode(aws_iam_role.job["ledger"].assume_role_policy).Statement :
      st.Principal == { Service = "ecs-tasks.amazonaws.com" } ? st.Condition.StringEquals["aws:SourceAccount"] == "111122223333" : st.Principal == { AWS = aws_iam_role.lambda_jobs.arn }
    ])
    error_message = "job roles trust ECS tasks of this account and the built-in jobs Lambda role only"
  }
}

run "assessment_role_is_passed_only_to_dms" {
  command = apply

  variables {
    assessment = {
      role_arn = "arn:aws:iam::111122223333:role/dms-assessment"
      bucket   = "dms-assessment-results"
      folder   = "assessments/dms-dev"
      enabled  = true
    }
  }

  assert {
    condition     = one([for st in jsondecode(aws_iam_role_policy.migrate.policy).Statement : st if st.Sid == "PassAssessmentRole"]).Resource == ["arn:aws:iam::111122223333:role/dms-assessment"] && one([for st in jsondecode(aws_iam_role_policy.migrate.policy).Statement : st if st.Sid == "PassAssessmentRole"]).Condition.StringEquals["iam:PassedToService"] == "dms.amazonaws.com"
    error_message = "iam:PassRole only on the assessment role and only to DMS"
  }

  assert {
    condition     = length([for p in [aws_iam_role_policy.post, aws_iam_role_policy.job_runner, aws_iam_role_policy.lambda_core, aws_iam_role_policy.lambda_waiter] : p if strcontains(p.policy, "dms-assessment")]) == 0
    error_message = "no other role may pass the assessment role"
  }

  assert {
    condition     = jsondecode(aws_ssm_parameter.task["ledger/core"].value).assessment.folder == "assessments/dms-dev/core"
    error_message = "the registration carries the assessment settings, folder per task"
  }
}

# dump.method = snapshot. Only tasks with snapshot_dumps give their project's
# job role snapshot rights: create on the target cluster, and on snapshots only with the project tag.
run "snapshot_dumps_are_scoped_by_the_project_tag" {
  command = apply

  variables {
    job_secret_arns = [
      "arn:aws:secretsmanager:eu-west-2:111122223333:secret:dms/ledger/target/runner-XyZ123",
      "arn:aws:secretsmanager:eu-west-2:111122223333:secret:dms/cards/target/runner-AbC456",
    ]
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
      cards = {
        tasks = {
          extra = {
            connections = {
              target = {
                engine      = "postgresql"
                secret_arn  = "arn:aws:secretsmanager:eu-west-2:111122223333:secret:dms/cards/target/runner-AbC456"
                cluster_arn = "arn:aws:rds:eu-west-2:111122223333:cluster:cards-target"
              }
            }
          }
        }
      }
    }
  }

  assert {
    condition     = one([for st in jsondecode(aws_iam_role_policy.job["ledger"].policy).Statement : st.Resource if st.Sid == "SnapshotTargetClusters"]) == ["arn:aws:rds:eu-west-2:111122223333:cluster:ledger-target"]
    error_message = "snapshots only of the target clusters of the project's snapshot tasks"
  }

  assert {
    condition = alltrue([
      for st in jsondecode(aws_iam_role_policy.job["ledger"].policy).Statement : (
        st.Sid == "CreateProjectSnapshots" ? st.Resource == ["arn:aws:rds:eu-west-2:111122223333:cluster-snapshot:ledger-*"] && st.Condition.StringEquals["aws:RequestTag/dms-orch:project"] == "ledger" && toset(st.Action) == toset(["rds:CreateDBClusterSnapshot", "rds:AddTagsToResource"]) : true
      )
    ]) && length([for st in jsondecode(aws_iam_role_policy.job["ledger"].policy).Statement : st if st.Sid == "CreateProjectSnapshots"]) == 1
    error_message = "a new snapshot must carry the project tag and the project prefix"
  }

  assert {
    condition = alltrue([
      for st in jsondecode(aws_iam_role_policy.job["ledger"].policy).Statement : (
        st.Sid == "ManageProjectSnapshots" ? st.Condition.StringEquals["rds:cluster-snapshot-tag/dms-orch:project"] == "ledger" && toset(st.Action) == toset(["rds:DescribeDBClusterSnapshots", "rds:DeleteDBClusterSnapshot"]) : true
      )
    ]) && length([for st in jsondecode(aws_iam_role_policy.job["ledger"].policy).Statement : st if st.Sid == "ManageProjectSnapshots"]) == 1
    error_message = "describe and delete only snapshots with the project tag"
  }

  assert {
    condition     = length([for st in jsondecode(aws_iam_role_policy.job["cards"].policy).Statement : st if length([for a in flatten([st.Action]) : a if startswith(a, "rds:")]) > 0]) == 0
    error_message = "no snapshot rights for a project without snapshot_dumps"
  }

  assert {
    condition     = jsondecode(aws_ssm_parameter.task["ledger/core"].value).defaults.snapshotDumps == true && jsondecode(aws_ssm_parameter.task["ledger/core"].value).defaults.dumpRetentionDays == 14
    error_message = "the registration tells resolve_context whether snapshots are allowed, and the retention"
  }

  assert {
    condition     = jsondecode(aws_ssm_parameter.task["cards/extra"].value).connections.target.clusterArn == "arn:aws:rds:eu-west-2:111122223333:cluster:cards-target"
    error_message = "the registration carries the cluster ARN of a connection"
  }
}

# resolve_context picks the runtime of the target check (verify-target on
# Fargate needs the sql-runner image).
run "resolve_context_knows_the_fargate_images" {
  command = apply

  assert {
    condition     = jsondecode(aws_lambda_function.this["resolve_context"].environment[0].variables.FARGATE_IMAGES) == ["sql-runner"]
    error_message = "resolve_context gets the image keys (no URIs needed)"
  }
}

# An IAM connection attaches the upstream aurora module's rds-db:connect policy
# to the project's job role; DMS and the Data API keep their secrets.
run "iam_database_auth_for_fargate_jobs" {
  command = apply

  variables {
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
              target_iam = {
                engine         = "postgresql"
                auth           = "iam"
                iam_user       = "orch_iam"
                host           = "ledger-target.cluster-abc.eu-west-2.rds.amazonaws.com"
                port           = 5432
                database       = "ledger"
                iam_policy_arn = "arn:aws:iam::111122223333:policy/ledger-target-eu-west-2-dbuser-orch_iam"
              }
            }
          }
        }
      }
    }
  }

  assert {
    condition     = aws_iam_role_policy_attachment.job_iam_database["ledger/core/target_iam"].policy_arn == "arn:aws:iam::111122223333:policy/ledger-target-eu-west-2-dbuser-orch_iam" && aws_iam_role_policy_attachment.job_iam_database["ledger/core/target_iam"].role == aws_iam_role.job["ledger"].name
    error_message = "the rds-db:connect policy goes to the project's job role"
  }

  assert {
    condition     = length(aws_iam_role_policy_attachment.job_iam_database) == 1
    error_message = "only IAM connections get a policy attachment"
  }

  assert {
    condition = (
      jsondecode(aws_ssm_parameter.task["ledger/core"].value).connections.target_iam.auth == "iam"
      && jsondecode(aws_ssm_parameter.task["ledger/core"].value).connections.target_iam.iamUser == "orch_iam"
      && jsondecode(aws_ssm_parameter.task["ledger/core"].value).connections.target_iam.secretArn == null
      && jsondecode(aws_ssm_parameter.task["ledger/core"].value).connections.target.auth == "secret"
    )
    error_message = "the registration says how each connection logs in; an IAM connection has no secret"
  }

  assert {
    condition     = one([for st in jsondecode(aws_iam_role_policy.job["ledger"].policy).Statement : st.Resource if st.Sid == "ReadProjectSecrets"]) == ["arn:aws:secretsmanager:eu-west-2:111122223333:secret:dms/ledger/target/runner-XyZ123"]
    error_message = "secrets only of the secret connections"
  }
}
