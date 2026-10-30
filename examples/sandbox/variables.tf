variable "region" {
  description = "Sandbox region."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Prefix for sandbox resources."
  type        = string
  default     = "dms-orch-sbx"
}

variable "project_tag" {
  description = "Value of the 'project' tag on every resource (cost tracking and cleanup)."
  type        = string
  default     = "dms-orch-sandbox"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "aurora_engine_version" {
  description = "Aurora PostgreSQL version (exact; min 0 ACU auto-pause needs 16.3+)."
  type        = string
  default     = "16.8"
}

variable "availability_zones" {
  description = "Two AZs where every needed interface endpoint exists (dms: 1b, 1c, 1d)."
  type        = list(string)
  default     = ["us-east-1b", "us-east-1c"]
}

variable "operator_cidr" {
  description = "Operator IP (/32) allowed to reach the databases for seeding and checks."
  type        = string
}

variable "dms_instance_class" {
  type    = string
  default = "dms.t3.small"
}

variable "dms_engine_version" {
  type    = string
  default = "3.5.4"
}

variable "interface_endpoints" {
  description = "Interface VPC endpoints for the Lambdas (no NAT). One subnet only to keep the cost down."
  type        = list(string)
  default     = ["states", "dms", "ssm", "sns", "logs", "sts", "secretsmanager", "rds-data"]
}

variable "ssm_runner" {
  description = "SSM runner instance (ssm_runner.tf) and the module's SSM runtime: pg-dump, restore and e2e-ssm jobs run on it (TF_VAR_ssm_runner=true)."
  type        = bool
  default     = false
}

variable "runner_ami_id" {
  description = "AMI of the SSM runner: the team AMI (psql, pg_dump, pg_restore, bash, python3 with boto3). Empty = latest Amazon Linux 2023 as a stand-in, the tools installed at boot from its own repository."
  type        = string
  default     = ""
}

variable "sql_runner" {
  description = "Fargate sql-runner (sql_runner.tf): ECR repository and CodeBuild project."
  type        = bool
  default     = false
}

variable "sql_runner_image_tag" {
  description = "Tag of a built sql-runner image in that repository: adds the Fargate task definition and the ECR endpoints."
  type        = string
  default     = ""

  validation {
    condition     = var.sql_runner_image_tag == "" || can(regex("^[0-9a-f]{7,40}$", var.sql_runner_image_tag))
    error_message = "sql_runner_image_tag must be a git SHA (immutable tag)."
  }
}

variable "trivy_image" {
  description = "Trivy image the CodeBuild project scans sql-runner with (ECR Public mirror, no Docker Hub limits)."
  type        = string
  default     = "public.ecr.aws/aquasecurity/trivy:0.67.2"
}

variable "assessment" {
  description = "Premigration assessment (scenario S14): report bucket, the dms module's assessment role, the orchestrator's assessment step."
  type        = bool
  default     = false
}

variable "snapshot_dumps" {
  description = "Scenario s10snap: dump.method = snapshot allowed on the task (job role snapshot rights) and the rds interface endpoint for the built-in jobs Lambda."
  type        = bool
  default     = false
}

variable "iam_auth" {
  description = "Scenario s1iam: IAM database authentication on the target (user orch_iam, created by the aurora module) and a target_iam connection for the Fargate jobs."
  type        = bool
  default     = false
}

variable "sqs_fanout" {
  description = "Scenario S16: an SQS queue subscribed to the run topic with a filter policy on severity = high."
  type        = bool
  default     = false
}
