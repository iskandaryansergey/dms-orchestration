############################################
# SSM runner instance, off by default (TF_VAR_ssm_runner=true): the module's SSM runtime runs
# pg-dump, orch.py restore and the e2e-ssm jobs on it (e2e s1ssm, s10).
#
# The target platform builds it from the team AMI (var.runner_ami_id). Without one the
# sandbox takes the latest Amazon Linux 2023 as a stand-in and installs the PostgreSQL client
# and boto3 at boot from the AL2023 repository (served from S3: the S3 gateway endpoint).
# No public IP, IMDSv2 only, the jobs security group (443 to the endpoints, 5432 to Aurora).
# Everything here goes with `terraform destroy`.
############################################

data "aws_ssm_parameter" "al2023" {
  count = var.ssm_runner && var.runner_ami_id == "" ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  ssm_runner_ami = var.runner_ami_id != "" ? var.runner_ami_id : one(data.aws_ssm_parameter.al2023[*].value)
  # Stand-in only: the team AMI has the tools already. The client major follows the Aurora engine.
  ssm_runner_user_data = var.runner_ami_id != "" ? null : <<-EOT
    #!/bin/bash
    set -euo pipefail
    dnf install -y "postgresql${split(".", var.aurora_engine_version)[0]}" python3-boto3
  EOT
}

resource "aws_iam_role" "ssm_runner" {
  count = var.ssm_runner ? 1 : 0
  name  = "${var.name}-ssm-runner"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm_runner_core" {
  count      = var.ssm_runner ? 1 : 0
  role       = aws_iam_role.ssm_runner[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Runtime zip, the project job roles, command output: the module's contract for the profile.
resource "aws_iam_role_policy" "ssm_runner" {
  count  = var.ssm_runner ? 1 : 0
  name   = "dms-orchestration-runner"
  role   = aws_iam_role.ssm_runner[0].id
  policy = module.dms_orchestration.ssm_runner_policy_json
}

resource "aws_iam_instance_profile" "ssm_runner" {
  count = var.ssm_runner ? 1 : 0
  name  = "${var.name}-ssm-runner"
  role  = aws_iam_role.ssm_runner[0].name
}

resource "aws_instance" "ssm_runner" {
  count                       = var.ssm_runner ? 1 : 0
  ami                         = local.ssm_runner_ami
  instance_type               = "t3.small"
  subnet_id                   = aws_subnet.public[0].id
  vpc_security_group_ids      = [module.dms_orchestration.jobs_security_group_id]
  iam_instance_profile        = aws_iam_instance_profile.ssm_runner[0].name
  associate_public_ip_address = false
  user_data                   = local.ssm_runner_user_data

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    encrypted   = true
    volume_size = 30
  }

  tags = { Name = "${var.name}-ssm-runner" }
}
