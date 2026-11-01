# The bucket has every control of the dms-orchestration bucket_requirements output.
# Mocked AWS provider: no credentials, nothing created.

mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }

  mock_resource "aws_s3_bucket" {
    defaults = { id = "platform-dms-artifacts", arn = "arn:aws:s3:::platform-dms-artifacts" }
  }
}

variables {
  name = "platform-dms-artifacts"
}

run "bucket_has_the_required_controls" {
  command = apply

  assert {
    condition     = aws_s3_bucket_versioning.this[0].versioning_configuration[0].status == "Enabled"
    error_message = "versioning is on: post loads context.json by the version migrate recorded"
  }

  assert {
    condition = alltrue([
      for r in aws_s3_bucket_server_side_encryption_configuration.this[0].rule :
      r.apply_server_side_encryption_by_default[0].sse_algorithm == "aws:kms" && r.bucket_key_enabled
    ])
    error_message = "default encryption is SSE-KMS with the bucket key"
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.this[0].block_public_acls,
      aws_s3_bucket_public_access_block.this[0].block_public_policy,
      aws_s3_bucket_public_access_block.this[0].ignore_public_acls,
      aws_s3_bucket_public_access_block.this[0].restrict_public_buckets,
      one([for r in aws_s3_bucket_ownership_controls.this[0].rule : r.object_ownership]) == "BucketOwnerEnforced",
    ])
    error_message = "no public access, no ACLs"
  }

  assert {
    condition     = one([for st in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : st.Condition.Bool["aws:SecureTransport"] if st.Sid == "DenyInsecureTransport"]) == "false"
    error_message = "TLS only"
  }

  assert {
    condition = alltrue([
      one([for st in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : st.Condition.StringNotEquals["s3:x-amz-server-side-encryption"] if st.Sid == "DenyDumpsWithoutKms"]) == "aws:kms",
      jsonencode(one([for st in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : st.Resource if st.Sid == "DenyDumpsWithoutKms"])) == jsonencode(["arn:aws:s3:::platform-dms-artifacts/dumps/*"]),
    ])
    error_message = "dumps/ accept only SSE-KMS"
  }

  assert {
    condition = alltrue([
      one([for r in aws_s3_bucket_lifecycle_configuration.this[0].rule : r.expiration[0].days if r.filter[0].prefix == "dumps/"]) == 14,
      one([for r in aws_s3_bucket_lifecycle_configuration.this[0].rule : r.abort_incomplete_multipart_upload[0].days_after_initiation if r.filter[0].prefix == "dumps/"]) == 1,
      one([for r in aws_s3_bucket_lifecycle_configuration.this[0].rule : r.expiration[0].days if r.filter[0].prefix == "runs/"]) == 365,
      one([for r in aws_s3_bucket_lifecycle_configuration.this[0].rule : r.noncurrent_version_expiration[0].noncurrent_days if r.filter[0].prefix == "jobs/"]) == 90,
      one([for r in aws_s3_bucket_lifecycle_configuration.this[0].rule : r.abort_incomplete_multipart_upload[0].days_after_initiation if r.filter[0].prefix == ""]) == 7,
    ])
    error_message = "dumps/ expire after dump_retention_days (14), runs/ after report_retention_days (365), old job set versions after 90 days, incomplete uploads bucket-wide after 7 days"
  }

  assert {
    condition     = !aws_s3_bucket.this[0].force_destroy && output.bucket_name == "platform-dms-artifacts"
    error_message = "a long-lived bucket: no force_destroy; its name is the artifacts_bucket_name of dms-orchestration"
  }
}

run "customer_managed_key" {
  command = apply

  variables {
    kms_key_arn         = "arn:aws:kms:eu-west-2:111122223333:key/11111111-2222-3333-4444-555555555555"
    dump_retention_days = 7
  }

  assert {
    condition     = one([for r in aws_s3_bucket_server_side_encryption_configuration.this[0].rule : r.apply_server_side_encryption_by_default[0].kms_master_key_id]) == var.kms_key_arn
    error_message = "default encryption uses the CMK"
  }

  assert {
    condition     = one([for r in aws_s3_bucket_lifecycle_configuration.this[0].rule : r.expiration[0].days if r.filter[0].prefix == "dumps/"]) == 7
    error_message = "dump retention follows the variable"
  }
}

# Shared module conventions: a create switch, non-nullable inputs, "" for unset optional
# strings, ManagedBy/Module tags.
run "conventions" {
  command = apply

  variables {
    kms_key_arn = null
    tags        = { CostCentre = "dms" }
  }

  assert {
    condition     = var.create && var.kms_key_arn == "" && one([for r in aws_s3_bucket_server_side_encryption_configuration.this[0].rule : r.apply_server_side_encryption_by_default[0].kms_master_key_id]) == null
    error_message = "kms_key_arn defaults to \"\" (the AWS managed key) and reaches the bucket as null"
  }

  assert {
    condition     = aws_s3_bucket.this[0].tags["ManagedBy"] == "terraform" && aws_s3_bucket.this[0].tags["Module"] == "artifacts-bucket" && aws_s3_bucket.this[0].tags["CostCentre"] == "dms"
    error_message = "the bucket carries ManagedBy = terraform and Module = artifacts-bucket besides the caller's tags"
  }
}

run "create_false_creates_nothing" {
  command = apply

  variables {
    create = false
  }

  assert {
    condition = alltrue([for n in [
      length(aws_s3_bucket.this), length(aws_s3_bucket_ownership_controls.this), length(aws_s3_bucket_public_access_block.this),
      length(aws_s3_bucket_versioning.this), length(aws_s3_bucket_server_side_encryption_configuration.this),
      length(aws_s3_bucket_lifecycle_configuration.this), length(aws_s3_bucket_policy.this),
    ] : n == 0]) && output.bucket_name == null && output.bucket_arn == null
    error_message = "create = false creates nothing and leaves the outputs null"
  }
}
