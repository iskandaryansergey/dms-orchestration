############################################
# Artifacts bucket for dms-orchestration in a long-lived workspace: job sets (jobs/), run
# artifacts (runs/) and target dumps (dumps/). The controls are those of the bucket_requirements
# output of dms-orchestration (and of the bucket it creates in storage.tf).
############################################

data "aws_partition" "current" {}

locals {
  create     = var.create
  bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${var.name}"
  tags       = merge(var.tags, { ManagedBy = "terraform", Module = "artifacts-bucket" })
}

# Access logging and cross-region replication are landing-zone decisions (.checkov.yaml).
resource "aws_s3_bucket" "this" {
  # checkov 3.2.334 does not link count-indexed resources of a root module to this bucket, so its graph
  # checks miss the resources below; tests/bucket.tftest.hcl asserts each control.
  # checkov:skip=CKV2_AWS_6:public access block is aws_s3_bucket_public_access_block.this (all four flags on)
  # checkov:skip=CKV_AWS_21:versioning is aws_s3_bucket_versioning.this (Enabled)
  # checkov:skip=CKV2_AWS_61:lifecycle is aws_s3_bucket_lifecycle_configuration.this
  count         = local.create ? 1 : 0
  bucket        = var.name
  force_destroy = var.force_destroy
  tags          = local.tags
}

resource "aws_s3_bucket_ownership_controls" "this" {
  count  = local.create ? 1 : 0
  bucket = aws_s3_bucket.this[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  count                   = local.create ? 1 : 0
  bucket                  = aws_s3_bucket.this[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Required: post loads context.json by the version id migrate recorded.
resource "aws_s3_bucket_versioning" "this" {
  count  = local.create ? 1 : 0
  bucket = aws_s3_bucket.this[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  count  = local.create ? 1 : 0
  bucket = aws_s3_bucket.this[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn == "" ? null : var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  count  = local.create ? 1 : 0
  bucket = aws_s3_bucket.this[0].id

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

  # Parts of an upload that never completed are billed and invisible: drop them bucket-wide.
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {
      prefix = ""
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_policy" "this" {
  count  = local.create ? 1 : 0
  bucket = aws_s3_bucket.this[0].id
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
  depends_on = [aws_s3_bucket_public_access_block.this]
}
