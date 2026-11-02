# Stands in for a runner module such as dms-sql-bastion: its own role and instance, plus the
# policy the caller passes in as a separate resource (additional_iam_policy_json).
terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "name" {
  type = string
}

variable "additional_iam_policy_json" {
  type = string
}

resource "aws_iam_role" "this" {
  name = "${var.name}-sql-bastion"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "additional" {
  name   = "additional"
  role   = aws_iam_role.this.id
  policy = var.additional_iam_policy_json
}

resource "aws_iam_instance_profile" "this" {
  name = "${var.name}-sql-bastion"
  role = aws_iam_role.this.name
}

resource "aws_instance" "this" {
  # checkov:skip=CKV_AWS_79:test fixture, never applied
  # checkov:skip=CKV_AWS_8:test fixture, never applied
  # checkov:skip=CKV_AWS_126:test fixture, never applied
  # checkov:skip=CKV_AWS_135:test fixture, never applied
  # checkov:skip=CKV2_AWS_41:test fixture, never applied
  ami                  = "ami-0123456789abcdef0"
  instance_type        = "t3.small"
  iam_instance_profile = aws_iam_instance_profile.this.name
  tags                 = { Migration = var.name }
}

output "instance_id" {
  value = aws_instance.this.id
}

output "role_arn" {
  value = aws_iam_role.this.arn
}
