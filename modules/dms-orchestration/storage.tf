############################################
# DynamoDB — single table: LOCK, RUN, EVENT, JOB, MIGRATION, TARGET, TABLE items (ADR 0004, 0005)
############################################

resource "aws_dynamodb_table" "this" {
  # Target platform: the journal (locks, runs, baselines, table copies) lives in a long-lived workspace and
  # comes in through table_name, so destroying the DMS workspace keeps it (ADR 0006).
  count                       = local.create_table ? 1 : 0
  name                        = local.prefix
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  range_key                   = "sk"
  deletion_protection_enabled = var.table_deletion_protection

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  attribute {
    name = "gsi1pk"
    type = "S"
  }
  attribute {
    name = "gsi1sk"
    type = "S"
  }
  attribute {
    name = "gsi2pk"
    type = "S"
  }
  attribute {
    name = "gsi2sk"
    type = "S"
  }

  # Run history per project.
  global_secondary_index {
    name            = "gsi1"
    hash_key        = "gsi1pk"
    range_key       = "gsi1sk"
    projection_type = "ALL"
  }

  # Sparse index of armed migrations (MIGRATION#ACTIVE) and locks (LOCK#ACTIVE) for the watchdog.
  global_secondary_index {
    name            = "gsi2"
    hash_key        = "gsi2pk"
    range_key       = "gsi2sk"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  tags = local.tags
}

############################################
# S3 — job sets (jobs/<project>/<version>/) and run artifacts (runs/<project>/<runId>/); versioned:
# post loads context.json by the version migrate recorded
############################################

resource "aws_s3_bucket" "artifacts" {
  count         = local.create_bucket ? 1 : 0
  bucket        = local.bucket_name
  force_destroy = var.artifacts_force_destroy
  tags          = local.tags
}

resource "aws_s3_bucket_ownership_controls" "artifacts" {
  count  = local.create_bucket ? 1 : 0
  bucket = aws_s3_bucket.artifacts[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  count                   = local.create_bucket ? 1 : 0
  bucket                  = aws_s3_bucket.artifacts[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  count  = local.create_bucket ? 1 : 0
  bucket = aws_s3_bucket.artifacts[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  count  = local.create_bucket ? 1 : 0
  bucket = aws_s3_bucket.artifacts[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = local.use_cmk ? "aws:kms" : "AES256"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = local.use_cmk
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  count  = local.create_bucket ? 1 : 0
  bucket = aws_s3_bucket.artifacts[0].id

  rule {
    id     = "expire-run-artifacts"
    status = "Enabled"
    filter {
      prefix = "runs/"
    }
    expiration {
      days = var.report_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  rule {
    id     = "old-job-set-versions"
    status = "Enabled"
    filter {
      prefix = "jobs/"
    }
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }

  rule {
    id     = "target-dumps"
    status = "Enabled"
    filter {
      prefix = "dumps/"
    }
    expiration {
      days = var.dump_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  count  = local.create_bucket ? 1 : 0
  bucket = aws_s3_bucket.artifacts[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
      }, {
      # Target dumps hold data: SSE-KMS only.
      Sid       = "DenyDumpsWithoutKms"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:PutObject"
      Resource  = ["${local.bucket_arn}/dumps/*"]
      Condition = { StringNotEquals = { "s3:x-amz-server-side-encryption" = "aws:kms" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}

############################################
# SNS — run notifications and alarms
############################################

resource "aws_sns_topic" "this" {
  count = local.create_topic ? 1 : 0
  name  = "${local.prefix}-events"
  # Not alias/aws/sns: its key policy cannot let CloudWatch alarms publish, so alarms
  # would silently never arrive. Without a CMK (sandbox) the topic is unencrypted;
  # with one, kms_key_policy_requirements lists cloudwatch.amazonaws.com.
  kms_master_key_id = local.use_cmk ? var.kms_key_arn : null
  tags              = local.tags
}

resource "aws_sns_topic_policy" "this" {
  count = local.create_topic ? 1 : 0
  arn   = aws_sns_topic.this[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudWatchAlarms"
      Effect    = "Allow"
      Principal = { Service = "cloudwatch.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.this[0].arn
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

############################################
# SSM — task registrations read by resolve_context (ARNs and metadata only, no secret values)
############################################

resource "aws_ssm_parameter" "task" {
  # checkov:skip=CKV2_AWS_34:registrations hold ARNs and metadata only, never secret values (connections carry secret ARNs)
  for_each    = local.task_registrations
  name        = "${local.ssm_prefix}/${each.key}"
  description = "DMS orchestration registration of ${each.key} (no secret values)"
  type        = "String"
  tier        = length(jsonencode(each.value)) > 4000 ? "Advanced" : "Standard"
  value       = jsonencode(each.value)
  tags        = local.tags
}
