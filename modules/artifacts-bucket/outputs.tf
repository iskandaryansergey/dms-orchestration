output "bucket_name" {
  description = "Bucket name: pass it to dms-orchestration as artifacts_bucket_name. null with create = false."
  value       = one(aws_s3_bucket.this[*].id)
}

output "bucket_arn" {
  description = "Bucket ARN."
  value       = one(aws_s3_bucket.this[*].arn)
}
