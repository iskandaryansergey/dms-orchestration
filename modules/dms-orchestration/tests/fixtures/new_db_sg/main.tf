# The database security group is created in the same plan (e.g. by the upstream
# aurora module), so its id is unknown while planning.
terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

resource "aws_security_group" "db" {
  # checkov:skip=CKV2_AWS_5:test fixture, stands in for the database security group of another module
  name        = "db"
  description = "Test fixture: database security group created in the same plan"
  vpc_id      = "vpc-0123456789abcdef0"
}

module "orch" {
  source = "../../.."

  environment = "dev"
  vpc_id      = "vpc-0123456789abcdef0"
  subnet_ids  = ["subnet-aaaa1111", "subnet-bbbb2222"]
  dms = {
    replication_instance_arn = "arn:aws:dms:eu-west-2:111122223333:rep:RI1"
    replication_instance_id  = "dms-dev-ri"
    task_arns                = { core = "arn:aws:dms:eu-west-2:111122223333:task:CORE1" }
  }
  projects                    = { ledger = { tasks = { core = {} } } }
  database_security_group_ids = [aws_security_group.db.id]
  acknowledged_checks         = ["own_sns_topic_outside_sandbox", "state_in_the_dms_workspace", "dumps_without_cmk", "no_cmk", "lambda_reserved_concurrency", "no_fargate_images"]
}
