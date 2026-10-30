############################################
# Sandbox for end-to-end runs of the orchestrator.
#
# Cost first: no NAT gateway, Aurora Serverless v2 with min 0 ACU (auto-pause), one
# small single-AZ DMS instance, interface endpoints in one subnet only. Databases are
# not reachable from the internet; seed data goes in through the RDS Data API.
# Aurora and DMS come from the upstream aurora/dms modules, fetched by `sbx fetch-upstream`
# into ./upstream (see upstream.env.example); the orchestrator consumes them through the
# `dms = {...}` contract only (docs/upstream-modules-contract.md).
# Always `terraform destroy` after a run.
############################################

data "aws_caller_identity" "current" {}

locals {
  azs            = var.availability_zones
  account_id     = data.aws_caller_identity.current.account_id
  sql_lambda     = "${var.name}-sql"
  sql_lambda_arn = "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.sql_lambda}"
  # Known at plan time: the dms module validates the name when the assessment is enabled.
  assessment_bucket = "${var.name}-assessment-${local.account_id}"
}

# Bucket for the premigration assessment reports (S14); gone with destroy.
resource "aws_s3_bucket" "assessment" {
  count         = var.assessment ? 1 : 0
  bucket        = local.assessment_bucket
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "assessment" {
  count                   = var.assessment ? 1 : 0
  bucket                  = aws_s3_bucket.assessment[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

############################################
# Network: public subnets, no NAT, endpoints for the Lambdas
############################################

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false
  tags                    = { Name = "${var.name}-public-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_vpc_endpoint" "gateway" {
  for_each          = toset(["s3", "dynamodb"])
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
  tags              = { Name = "${var.name}-${each.key}" }
}

resource "aws_security_group" "endpoints" {
  name        = "${var.name}-endpoints"
  description = "Interface endpoints: HTTPS from the VPC"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${var.name}-endpoints" }
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_https" {
  security_group_id = aws_security_group.endpoints.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = var.vpc_cidr
}

resource "aws_vpc_endpoint" "interface" {
  # rds comes from the module's network_contract (required when a task has snapshot_dumps).
  for_each            = toset(concat(var.interface_endpoints, local.sql_runner_image != "" ? ["ecr.api", "ecr.dkr"] : [], [for e in module.dms_orchestration.network_contract.required_vpc_endpoints : e if e == "rds"]))
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.public[0].id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  tags                = { Name = "${var.name}-${each.key}" }
}

############################################
# Aurora PostgreSQL Serverless v2 source and target: the upstream aurora module (commit
# pinned by UPSTREAM_MODULES_REF; it needs IAM database users created through the Data
# API). min 0 ACU = auto-pause after 5 idle minutes (no compute bill), Data API on.
# Reachable from DMS, the orchestrator jobs, the sandbox SQL Lambda and the operator IP
# (seeding from the laptop) only.
############################################

module "aurora" {
  # upstream modules, fetched by `sbx fetch-upstream` (see upstream.env.example)
  source   = "./upstream/modules/aurora"
  for_each = toset(["source", "target"])

  name                   = "${var.name}-${each.key}"
  engine                 = "aurora-postgresql"
  engine_version         = var.aurora_engine_version
  parameter_group_family = "aurora-postgresql${split(".", var.aurora_engine_version)[0]}"
  instance_class         = "db.serverless"
  instances              = { writer = {} }
  serverlessv2_scaling_configuration = {
    min_capacity             = 0
    max_capacity             = 2
    seconds_until_auto_pause = 300
  }

  vpc_id              = aws_vpc.this.id
  subnet_ids          = aws_subnet.public[*].id
  publicly_accessible = true # operator seeding only; ingress is still the rules below
  allow_public_access = true
  ingress_rules = {
    dms        = { referenced_security_group_id = aws_security_group.dms.id, description = "DMS replication instance" }
    jobs       = { referenced_security_group_id = module.dms_orchestration.jobs_security_group_id, description = "orchestrator jobs" }
    sql-lambda = { referenced_security_group_id = aws_security_group.sql_lambda.id, description = "sandbox SQL Lambda" }
    operator   = { cidr_ipv4 = var.operator_cidr, description = "operator IP for seeding" }
  }

  database_name               = "ledger"
  master_username             = "orch_admin"
  manage_master_user_password = false # DMS and the jobs need host/port/dbname in their secrets
  master_password             = random_password.db[each.key].result
  allow_plaintext_credentials = true # sandbox: local state, destroyed after the run
  enable_data_api             = true

  # Scenario s1iam (var.iam_auth): token login on the target for the user orch_iam, created in
  # the engine by the module (Data API, admin secret below) with its rds-db:connect policy.
  iam_database_authentication_enabled = local.iam_target[each.key]
  iam_database_users                  = { for u in(local.iam_target[each.key] ? [local.iam_user] : []) : u => {} }
  create_iam_database_auth_policies   = local.iam_target[each.key]
  create_iam_database_users           = local.iam_target[each.key]
  iam_database_users_admin_secret     = { arn = local.iam_target[each.key] ? one(aws_secretsmanager_secret_version.db_admin[*].arn) : "" }

  # Throw-away: no protection, no snapshot, minimum backups, no paid monitoring.
  deletion_protection          = false
  skip_final_snapshot          = true
  delete_automated_backups     = true
  backup_retention_days        = 1
  performance_insights_enabled = false
  enhanced_monitoring_interval = 0
  create_monitoring_role       = false
  create_alarms                = false
  create_event_subscriptions   = false
  apply_immediately            = true

  acknowledged_checks = [
    "cluster_survives_an_az", "instances_are_spread_across_azs", "somebody_hears_a_failover",
    "deleting_the_cluster_is_protected", "deleting_the_cluster_leaves_a_snapshot", "backups_cover_a_week",
    "query_level_metrics_exist", "database_logs_leave_the_instance", "credentials_are_not_in_state",
    "ingress_stays_inside_private_address_space", # operator IP for seeding
    "iam_auth_failures_are_logged",               # s1iam: no log exports in the sandbox
  ]
}

locals {
  iam_user   = "orch_iam"
  iam_target = { source = false, target = var.iam_auth }
}

# The Data API login of the module's user-creating function. A secret of its own, without the
# cluster endpoint: the db secret of the target waits for the cluster, the function must not.
resource "aws_secretsmanager_secret" "db_admin" {
  count                   = var.iam_auth ? 1 : 0
  name                    = "${var.name}/target-admin"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "db_admin" {
  count         = var.iam_auth ? 1 : 0
  secret_id     = aws_secretsmanager_secret.db_admin[0].id
  secret_string = jsonencode({ username = "orch_admin", password = random_password.db["target"].result })
}

resource "random_password" "db" {
  for_each = toset(["source", "target", "dms_source"])
  length   = 32
  special  = false
}

# Secrets in the shape DMS (host/port/username/password) and the jobs (+ dbname) read.
resource "aws_secretsmanager_secret" "db" {
  for_each                = toset(["source", "target", "dms_source"])
  name                    = "${var.name}/${each.key}"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "db" {
  for_each  = aws_secretsmanager_secret.db
  secret_id = each.value.id
  secret_string = jsonencode({
    engine   = "postgres"
    username = each.key == "dms_source" ? "dms_source" : "orch_admin"
    password = random_password.db[each.key].result
    host     = module.aurora[each.key == "target" ? "target" : "source"].cluster_endpoint
    port     = 5432
    dbname   = "ledger"
  })
}

############################################
# DMS: the upstream dms module (same pinned commit). Provisioned, smallest class,
# single AZ, task created stopped (start = false): the orchestrator starts it, never
# terraform.
############################################

# RDS CA bundle for ssl_mode = verify-full.
data "http" "rds_ca" {
  url = "https://truststore.pki.rds.amazonaws.com/${var.region}/${var.region}-bundle.pem"
}

resource "aws_security_group" "dms" {
  name        = "${var.name}-dms"
  description = "DMS replication instance"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${var.name}-dms" }
}

resource "aws_vpc_security_group_egress_rule" "dms_all" {
  security_group_id = aws_security_group.dms.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

module "dms" {
  # upstream modules, fetched by `sbx fetch-upstream` (see upstream.env.example)
  source = "./upstream/modules/dms"

  name                         = var.name
  compute_mode                 = "provisioned"
  instance_class               = var.dms_instance_class
  allocated_storage_gb         = 20
  engine_version               = var.dms_engine_version
  multi_az                     = false
  subnet_ids                   = aws_subnet.public[*].id
  security_group_ids           = [aws_security_group.dms.id]
  create_account_service_roles = true # the sandbox account has none; a landing zone owns them on the target platform

  certificates = { rds = { pem = data.http.rds_ca.response_body } }

  endpoints = {
    source = {
      role            = "source"
      engine          = "aurora-postgresql"
      server_name     = module.aurora["source"].cluster_endpoint
      port            = 5432
      database_name   = "ledger"
      secret_arn      = aws_secretsmanager_secret.db["dms_source"].arn
      ssl_mode        = "verify-full"
      certificate_key = "rds"
    }
    target = {
      role            = "target"
      engine          = "aurora-postgresql"
      server_name     = module.aurora["target"].cluster_endpoint
      port            = 5432
      database_name   = "ledger"
      secret_arn      = aws_secretsmanager_secret.db["target"].arn
      ssl_mode        = "verify-full"
      certificate_key = "rds"
      # DMS sessions only: foreign keys are not enforced while tables load in parallel.
      postgres = { after_connect_script = "SET session_replication_role = replica;" }
    }
  }

  tasks = {
    core = {
      source_endpoint = "source"
      target_endpoint = "target"
      migration_type  = "full-load"
      start           = false
      table_mappings = jsonencode({
        rules = [{
          "rule-type"      = "selection"
          "rule-id"        = "1"
          "rule-name"      = "app"
          "object-locator" = { "schema-name" = "app", "table-name" = "%" }
          "rule-action"    = "include"
        }]
      })
      settings = {
        target_table_prep_mode = "DO_NOTHING" # the target schema is built before the run
        max_full_load_subtasks = 4
        lob_max_size_kb        = 64
        # validation_enabled stays at the module default (true): part of the contract under test.
      }
    }
  }

  log_retention_days         = 1
  log_group_skip_destroy     = false
  create_alarms              = false
  create_event_subscriptions = false

  # Scenario S14: the module's assessment role, scoped to the bucket below (var.assessment).
  premigration_assessment = {
    enabled     = var.assessment
    bucket_name = var.assessment ? local.assessment_bucket : ""
  }

  acknowledged_checks = [
    "somebody_hears_a_failed_task", "replication_survives_an_az", "full_load_does_not_leave_the_meter_running",
  ]

  depends_on = [aws_vpc_endpoint.interface] # DMS reads the endpoint secrets through the VPC endpoint
}

############################################
# Project Lambda for SQL (stands in for the Fargate sql-runner image, which needs a
# docker build): truncate on reset, row counts after the load. pg8000, in the VPC.
############################################

resource "terraform_data" "sql_lambda_deps" {
  triggers_replace = [filesha256("${path.module}/lambda/sandbox_sql.py")]
  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = "uv pip install --quiet --python-platform x86_64-manylinux2014 --target .build/sql_lambda pg8000==1.31.6 scramp==1.5.0 && cp lambda/sandbox_sql.py .build/sql_lambda/"
  }
}

data "archive_file" "sql_lambda" {
  type        = "zip"
  source_dir  = "${path.module}/.build/sql_lambda"
  output_path = "${path.module}/.build/sandbox_sql.zip"
  depends_on  = [terraform_data.sql_lambda_deps]
}

resource "aws_security_group" "sql_lambda" {
  name        = "${var.name}-sql-lambda"
  description = "Sandbox SQL Lambda: egress inside the VPC only"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_egress_rule" "sql_lambda_vpc" {
  security_group_id = aws_security_group.sql_lambda.id
  ip_protocol       = "-1"
  cidr_ipv4         = var.vpc_cidr
}

resource "aws_iam_role" "sql_lambda" {
  name = local.sql_lambda
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "sql_lambda_vpc" {
  role       = aws_iam_role.sql_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "sql_lambda" {
  name = "db-secrets"
  role = aws_iam_role.sql_lambda.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_secretsmanager_secret.db["source"].arn, aws_secretsmanager_secret.db["target"].arn] }]
  })
}

resource "aws_cloudwatch_log_group" "sql_lambda" {
  name              = "/aws/lambda/${local.sql_lambda}"
  retention_in_days = 1
}

resource "aws_lambda_function" "sql" {
  function_name    = local.sql_lambda
  role             = aws_iam_role.sql_lambda.arn
  runtime          = "python3.13"
  handler          = "sandbox_sql.handler"
  filename         = data.archive_file.sql_lambda.output_path
  source_code_hash = data.archive_file.sql_lambda.output_base64sha256
  timeout          = 300
  vpc_config {
    subnet_ids         = aws_subnet.public[*].id
    security_group_ids = [aws_security_group.sql_lambda.id]
  }
  environment {
    variables = {
      SOURCE_SECRET_ARN = aws_secretsmanager_secret.db["source"].arn
      TARGET_SECRET_ARN = aws_secretsmanager_secret.db["target"].arn
    }
  }
  depends_on = [aws_cloudwatch_log_group.sql_lambda, aws_iam_role_policy_attachment.sql_lambda_vpc]
}

############################################
# The orchestrator
############################################

module "dms_orchestration" {
  source = "../../modules/dms-orchestration"

  environment = "sandbox"
  vpc_id      = aws_vpc.this.id
  subnet_ids  = aws_subnet.public[*].id
  # The VPC is created in this same plan, so the module cannot read its CIDR for the
  # jobs SG egress for_each (Invalid for_each argument): pass it as a known value.
  https_egress_cidr_blocks = [var.vpc_cidr]

  # The upstream dms module outputs, same names (docs/upstream-modules-contract.md). Named one by
  # one: passing module.dms whole makes this module wait for the dms module's check
  # blocks, which Terraform reports as a dependency cycle.
  dms = {
    replication_instance_arn = module.dms.replication_instance_arn
    replication_instance_id  = module.dms.replication_instance_id
    task_arns                = module.dms.task_arns
    task_resource_ids        = module.dms.task_resource_ids
    endpoint_arns            = module.dms.endpoint_arns
    log_group_name           = module.dms.log_group_name
  }

  projects = {
    ledger = {
      tasks = {
        core = {
          connections = merge({
            source = {
              engine               = "postgresql"
              secret_arn           = aws_secretsmanager_secret.db["source"].arn
              host                 = module.aurora["source"].cluster_endpoint
              data_api_cluster_arn = module.aurora["source"].cluster_arn
              port                 = 5432
              database             = "ledger"
            }
            target = {
              engine               = "postgresql"
              secret_arn           = aws_secretsmanager_secret.db["target"].arn
              host                 = module.aurora["target"].cluster_endpoint
              data_api_cluster_arn = module.aurora["target"].cluster_arn
              port                 = 5432
              database             = "ledger"
            }
            dms_source = {
              engine               = "postgresql"
              secret_arn           = aws_secretsmanager_secret.db["dms_source"].arn
              host                 = module.aurora["source"].cluster_endpoint
              data_api_cluster_arn = module.aurora["source"].cluster_arn
              port                 = 5432
              database             = "ledger"
            }
            }, { for k, v in {
              # s1iam: the same target, token login (no secret) for the Fargate jobs
              target_iam = {
                engine         = "postgresql"
                auth           = "iam"
                iam_user       = local.iam_user
                host           = module.aurora["target"].cluster_endpoint
                port           = 5432
                database       = "ledger"
                iam_policy_arn = try(module.aurora["target"].iam_database_auth.users[local.iam_user].policy_arn, null)
              }
          } : k => v if var.iam_auth })
          # The e2e job sets are rebuilt per run (sandbox.py sync-jobs), so they are not pinned;
          # runs still check every file against the bundle.sha256 uploaded with them.
          jobs = { default_version = "e2e", allow_unpinned = true }
          # snapshot_dumps (s10snap): dump.method = snapshot, the job role may snapshot the target cluster.
          defaults = { target_reset_policy = "if-interrupted", run_timeout_seconds = 7200, fail_on_table_errors = true, snapshot_dumps = var.snapshot_dumps }
        }
      }
    }
  }

  job_secret_arns             = [for s in aws_secretsmanager_secret.db : s.arn]
  job_lambda_arns             = [local.sql_lambda_arn]
  database_security_group_ids = [for a in module.aurora : a.security_group_id]
  artifacts_force_destroy     = true
  table_deletion_protection   = false
  log_retention_days          = 1
  watchdog_interval_minutes   = 5
  acknowledged_checks         = ["no_cmk", "lambda_reserved_concurrency"]

  # Premigration assessment step (S14), from the dms module's output, off by default per run.
  assessment = var.assessment ? {
    role_arn = module.dms.premigration_assessment.role_arn
    bucket   = module.dms.premigration_assessment.bucket
    folder   = module.dms.premigration_assessment.folder
  } : null

  # sql_runner.tf: the image built by CodeBuild, when its tag is given.
  fargate = {
    images              = local.sql_runner_image == "" ? {} : { sql-runner = local.sql_runner_image }
    ecr_repository_arns = aws_ecr_repository.sql_runner[*].arn
  }
}

############################################
# Reconnaissance: every raw DMS event into a log group
############################################

resource "aws_cloudwatch_log_group" "dms_events" {
  name              = "/aws/events/${var.name}-dms"
  retention_in_days = 1
}

resource "aws_cloudwatch_log_resource_policy" "dms_events" {
  policy_name = "${var.name}-dms-events"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"] }
      Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource  = "${aws_cloudwatch_log_group.dms_events.arn}:*"
    }]
  })
}

resource "aws_cloudwatch_event_rule" "dms_all" {
  name          = "${var.name}-dms-all"
  description   = "Sandbox: record every DMS event"
  event_pattern = jsonencode({ source = ["aws.dms"] })
}

resource "aws_cloudwatch_event_target" "dms_all" {
  rule = aws_cloudwatch_event_rule.dms_all.name
  arn  = aws_cloudwatch_log_group.dms_events.arn
}
