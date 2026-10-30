variable "name" {
  description = "Bucket name. Pass it to dms-orchestration as artifacts_bucket_name."
  type        = string
}

variable "kms_key_arn" {
  description = "Customer managed key for default encryption (SSE-KMS). Null = the AWS managed key aws/s3."
  type        = string
  default     = null
}

variable "report_retention_days" {
  description = "Days to keep run artifacts under runs/ (same value as dms-orchestration report_retention_days)."
  type        = number
  default     = 365
}

variable "dump_retention_days" {
  description = "Days to keep target dumps under dumps/ (same value as dms-orchestration dump_retention_days)."
  type        = number
  default     = 14
}

variable "force_destroy" {
  description = "Allow destroying the bucket with objects in it. Keep false: the bucket outlives the DMS workspace."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags for the bucket."
  type        = map(string)
  default     = {}
}
