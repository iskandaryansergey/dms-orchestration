variable "create" {
  description = "Create the bucket. false = nothing is created and the outputs are null."
  type        = bool
  default     = true
  nullable    = false
}

variable "name" {
  description = "Bucket name. Pass it to dms-orchestration as artifacts_bucket_name."
  type        = string
  nullable    = false
}

variable "kms_key_arn" {
  description = "Customer managed key for default encryption (SSE-KMS). Empty = the AWS managed key aws/s3."
  type        = string
  default     = ""
  nullable    = false
}

variable "report_retention_days" {
  description = "Days to keep run artifacts under runs/ (same value as dms-orchestration report_retention_days)."
  type        = number
  default     = 365
  nullable    = false
}

variable "dump_retention_days" {
  description = "Days to keep target dumps under dumps/ (same value as dms-orchestration dump_retention_days)."
  type        = number
  default     = 14
  nullable    = false
}

variable "force_destroy" {
  description = "Allow destroying the bucket with objects in it. Keep false: the bucket outlives the DMS workspace."
  type        = bool
  default     = false
  nullable    = false
}

variable "tags" {
  description = "Tags for the bucket (ManagedBy and Module are added)."
  type        = map(string)
  default     = {}
  nullable    = false
}
