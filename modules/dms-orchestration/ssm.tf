############################################
# SSM Run Command runtime (runtime ssm, ADR 0011): long jobs on runner instances built from
# the team AMI. The job-runner sends the module document to one online runner; the document
# fetches the runtime zip from the artifacts bucket, checks its sha256 and runs orch-job as
# ssm.runner_user with the project's job role (--assume-role). Nothing comes from the internet.
############################################

locals {
  ssm_enabled       = var.ssm.enabled
  ssm_document_name = coalesce(var.ssm.document_name_override, "${local.prefix}-orch-job")
  ssm_document_arn  = "arn:${local.partition}:ssm:${local.region}:${local.account_id}:document/${local.ssm_document_name}"

  # DescribeInstanceInformation filters of the job-runner (FindSsmRunner): the configured runners
  # that are online. All filters apply together.
  ssm_instance_filters = concat(
    length(var.ssm.instance_ids) > 0 ? [{ Key = "InstanceIds", Values = var.ssm.instance_ids }] : [],
    [for k in sort(keys(var.ssm.target_tags)) : { Key = "tag:${k}", Values = [var.ssm.target_tags[k]] }],
    [{ Key = "PingStatus", Values = ["Online"] }],
  )
}
