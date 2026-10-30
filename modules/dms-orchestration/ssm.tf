############################################
# SSM Run Command runtime (runtime ssm, ADR 0011): long jobs on runner instances built from
# the team AMI. The job-runner sends the module document to one online runner; the document
# fetches the runtime zip from the artifacts bucket, checks its sha256 and runs orch-job as
# ssm.runner_user with the project's job role (--assume-role). Nothing comes from the internet.
############################################

locals {
  ssm_enabled       = local.create && var.ssm.enabled
  ssm_document_name = coalesce(var.ssm.document_name_override, "${local.prefix}-orch-job")
  ssm_document_arn  = "arn:${local.partition}:ssm:${local.region}:${local.account_id}:document/${local.ssm_document_name}"

  # DescribeInstanceInformation filters of the job-runner (FindSsmRunner): the configured runners
  # that are online. All filters apply together.
  ssm_instance_filters = concat(
    length(var.ssm.instance_ids) > 0 ? [{ Key = "InstanceIds", Values = var.ssm.instance_ids }] : [],
    [for k in sort(keys(var.ssm.target_tags)) : { Key = "tag:${k}", Values = [var.ssm.target_tags[k]] }],
    [{ Key = "PingStatus", Values = ["Online"] }],
  )

  # Runtime zip: src/jobs/sql-runner without the Dockerfile, plus the shared modules the image
  # copies (tests/python/test_sql_runner.py keeps both lists equal). Code for the AMI's python3.
  ssm_runtime_shared = ["__init__.py", "expr.py", "preflight.py", "fingerprint.py", "bundle.py", "errors.py"]
  ssm_runtime_dir    = "${path.module}/src/jobs/sql-runner"
  ssm_runtime_files = merge(
    {
      for f in fileset(local.ssm_runtime_dir, "**") : f => "${local.ssm_runtime_dir}/${f}"
      if f != "Dockerfile" && !strcontains(f, "__pycache__") && !endswith(f, ".pyc")
    },
    { for f in local.ssm_runtime_shared : "shared/${f}" => "${path.module}/src/shared/${f}" },
  )
  ssm_runtime_sha256 = local.ssm_enabled ? data.archive_file.ssm_runtime[0].output_sha256 : ""
  ssm_runtime_key    = "runtime/${local.ssm_runtime_sha256}/sql-runner.zip"

  # Document parameters: the job-runner fills them from the job spec; every value is checked
  # against a pattern before SSM puts it into the script, so none can carry shell syntax.
  ssm_document = {
    schemaVersion = "2.2"
    description   = "dms-orchestration: run one job with orch-job as ${var.ssm.runner_user} and the project's job role"
    parameters = {
      specS3Uri = {
        type           = "String"
        description    = "s3 URI of the job's spec.json (the command is read from it)"
        allowedPattern = "^s3://${replace(local.bucket_name, ".", "\\.")}/runs/[a-z][a-z0-9-]{0,39}/[A-Za-z0-9_-]{1,128}/jobs/[a-z_]{1,16}/[0-9]{2,4}-[a-z0-9][a-z0-9_-]{0,47}/spec\\.json$"
      }
      jobRoleArn = {
        type           = "String"
        description    = "the project's job role, assumed from the instance profile"
        allowedPattern = "^arn:${local.partition}:iam::${local.account_id}:role/${local.prefix}-job-[a-z][a-z0-9-]{0,39}$"
      }
      attempt          = { type = "String", default = "1", allowedPattern = "^[0-9]{1,3}$" }
      dryRun           = { type = "String", default = "0", allowedPattern = "^[01]$" }
      executionTimeout = { type = "String", default = "3600", allowedPattern = "^[1-9][0-9]{1,5}$" }
    }
    mainSteps = [{
      action       = "aws:runShellScript"
      name         = "orchJob"
      precondition = { StringEquals = ["platformType", "Linux"] }
      inputs = {
        timeoutSeconds = "{{ executionTimeout }}"
        runCommand = split("\n", trimspace(templatefile("${path.module}/ssm/orch-job.sh.tftpl", {
          document_name  = local.ssm_document_name
          runner_user    = var.ssm.runner_user
          bucket         = local.bucket_name
          runtime_key    = local.ssm_runtime_key
          runtime_sha256 = local.ssm_runtime_sha256
          region         = local.region
          environment    = var.environment
          dump_kms_key   = var.kms_key_arn
          log_group      = local.jobs_log_group_name
        })))
      }
    }]
  }

  # Not attached: what the runner instance profile needs besides SSM management
  # (AmazonSSMManagedInstanceCore or the platform's equivalent).
  ssm_runner_policy = {
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "ReadRuntime"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${local.bucket_arn}/runtime/*"]
      },
      {
        # orch-job assumes the job role of the run's project; secrets, specs and results are
        # reachable only through it.
        Sid      = "AssumeProjectJobRoles"
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = values(local.job_role_arns)
      },
      {
        # Command output (SendCommand CloudWatchOutputConfig) into the jobs log group.
        Sid      = "CommandOutput"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = ["${local.jobs_log_group_arn}:*"]
      },
      {
        Sid      = "FindLogGroup"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "*"
      },
    ], local.kms_statements)
  }
}

data "archive_file" "ssm_runtime" {
  count       = local.ssm_enabled ? 1 : 0
  type        = "zip"
  output_path = "${path.module}/.build/ssm-runtime-${local.prefix}.zip"

  dynamic "source" {
    for_each = local.ssm_runtime_files
    content {
      content  = file(source.value)
      filename = source.key
    }
  }
}

resource "aws_s3_object" "ssm_runtime" {
  count                  = local.ssm_enabled ? 1 : 0
  bucket                 = local.bucket_name
  key                    = local.ssm_runtime_key
  source                 = data.archive_file.ssm_runtime[0].output_path
  source_hash            = data.archive_file.ssm_runtime[0].output_base64sha256
  content_type           = "application/zip"
  server_side_encryption = local.use_cmk ? "aws:kms" : null
  kms_key_id             = local.kms_key_arn

  depends_on = [aws_s3_bucket_policy.artifacts, aws_s3_bucket_server_side_encryption_configuration.artifacts]
}

resource "aws_ssm_document" "orch_job" {
  count           = local.ssm_enabled && var.ssm.document_name_override == "" ? 1 : 0
  name            = local.ssm_document_name
  document_type   = "Command"
  document_format = "JSON"
  content         = jsonencode(local.ssm_document)
  tags            = local.tags
}
