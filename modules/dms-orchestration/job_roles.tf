############################################
# Job roles — one per project (trust model: docs/architecture.md, "Security").
#
# Pre/post jobs run project code against project databases. A job of one project
# must not read another project's secrets or job sets, so the secrets, Data API
# clusters and job-set prefixes live on a role per project:
#   * Fargate jobs get it as the task role (RunTask Overrides.TaskRoleArn);
#   * built-in Lambda jobs assume it (sts:AssumeRole) for their database calls.
# The shared ecs_task and lambda_jobs roles keep no secret access.
# ARNs are built from names (like the state machine ARNs) so policies can refer
# to them before the roles exist.
############################################

locals {
  job_role_names = { for p in keys(var.projects) : p => "${local.prefix}-job-${p}" }
  job_role_arns  = { for p, n in local.job_role_names : p => "arn:${local.partition}:iam::${local.account_id}:role/${n}" }

  project_secret_arns = {
    for p, proj in var.projects : p => distinct(flatten([
      for t in values(proj.tasks) : [for c in values(t.connections) : c.secret_arn if c.secret_arn != null]
    ]))
  }

  # auth = "iam" connections, keyed "<project>/<task>/<connection>" (known
  # at plan); the policy ARN itself may come from a module applied in the same plan.
  iam_database_connections = merge(flatten([
    for p, proj in var.projects : [
      for task_key, t in proj.tasks : {
        for name, c in t.connections : "${p}/${task_key}/${name}" => { project = p, policy_arn = c.iam_policy_arn } if c.auth == "iam"
      }
    ]
  ])...)

  project_data_api_arns = {
    for p, proj in var.projects : p => distinct(compact(flatten([
      for t in values(proj.tasks) : [for c in values(t.connections) : c.data_api_cluster_arn]
    ])))
  }

  # dump.method = snapshot: the target clusters of the project's tasks with
  # snapshot_dumps. Snapshots themselves are scoped by the project tag (job_role_policies).
  project_snapshot_clusters = {
    for p, proj in var.projects : p => distinct(compact([
      for t in values(proj.tasks) : try(coalesce(t.connections["target"].cluster_arn, t.connections["target"].data_api_cluster_arn), null) if t.defaults.snapshot_dumps
    ]))
  }

  project_job_prefixes = {
    for p in keys(var.projects) : p => coalescelist(
      distinct([for r in values(local.task_registrations) : trim(r.jobs.s3Prefix, "/") if r.project == p]),
      ["jobs/${p}"],
    )
  }

  job_role_assume = {
    for p in keys(var.projects) : p => jsonencode({
      Version = "2012-10-17"
      Statement = [
        merge(jsondecode(local.ecs_assume).Statement[0], { Sid = "EcsTasksOfThisAccount" }),
        {
          Sid       = "BuiltinJobsLambda"
          Effect    = "Allow"
          Principal = { AWS = aws_iam_role.lambda_jobs.arn }
          Action    = "sts:AssumeRole"
        },
      ]
    })
  }

  job_role_policies = {
    for p in keys(var.projects) : p => {
      Version = "2012-10-17"
      Statement = concat([
        {
          Sid      = "ReadJobBundles"
          Effect   = "Allow"
          Action   = ["s3:GetObject"]
          Resource = [for prefix in local.project_job_prefixes[p] : "${local.bucket_arn}/${prefix}/*"]
        },
        {
          Sid       = "ListJobBundles"
          Effect    = "Allow"
          Action    = ["s3:ListBucket"]
          Resource  = [local.bucket_arn]
          Condition = { StringLike = { "s3:prefix" = [for prefix in local.project_job_prefixes[p] : "${prefix}/*"] } }
        },
        {
          Sid      = "RunSpecsAndResults"
          Effect   = "Allow"
          Action   = ["s3:GetObject", "s3:PutObject"]
          Resource = ["${local.bucket_arn}/runs/*"]
        },
        {
          # pg-dump-s3 / pg-restore-s3: only this project's dumps
          Sid      = "ProjectDumps"
          Effect   = "Allow"
          Action   = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"]
          Resource = ["${local.bucket_arn}/dumps/${p}/*"]
        },
        ], length(local.project_secret_arns[p]) == 0 ? [] : [{
          Sid      = "ReadProjectSecrets"
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = local.project_secret_arns[p]
          }], length(local.project_data_api_arns[p]) == 0 ? [] : [{
          Sid      = "DataApi"
          Effect   = "Allow"
          Action   = ["rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement"]
          Resource = local.project_data_api_arns[p]
          }], length(local.project_snapshot_clusters[p]) == 0 ? [] : [{
          Sid      = "SnapshotTargetClusters"
          Effect   = "Allow"
          Action   = ["rds:CreateDBClusterSnapshot", "rds:DescribeDBClusterSnapshots"]
          Resource = local.project_snapshot_clusters[p]
          }], length(local.project_snapshot_clusters[p]) == 0 ? [] : [{
          # A new snapshot must carry the project tag (builtin dump/cluster_snapshot tags it).
          Sid       = "CreateProjectSnapshots"
          Effect    = "Allow"
          Action    = ["rds:CreateDBClusterSnapshot", "rds:AddTagsToResource"]
          Resource  = ["arn:${local.partition}:rds:${local.region}:${local.account_id}:cluster-snapshot:${p}-*"]
          Condition = { StringEquals = { "aws:RequestTag/dms-orch:project" = p } }
          }], length(local.project_snapshot_clusters[p]) == 0 ? [] : [{
          # Retention (cleanup/cluster_snapshots): only snapshots tagged with this project.
          Sid       = "ManageProjectSnapshots"
          Effect    = "Allow"
          Action    = ["rds:DescribeDBClusterSnapshots", "rds:DeleteDBClusterSnapshot"]
          Resource  = ["arn:${local.partition}:rds:${local.region}:${local.account_id}:cluster-snapshot:*"]
          Condition = { StringEquals = { "rds:cluster-snapshot-tag/dms-orch:project" = p } }
      }], local.kms_statements)
    }
  }
}

resource "aws_iam_role" "job" {
  for_each = local.job_role_names

  name                 = each.value
  assume_role_policy   = local.job_role_assume[each.key]
  permissions_boundary = local.boundary
  tags                 = merge(local.tags, { "dms-orch:project" = each.key })

  lifecycle {
    precondition {
      condition     = length(each.value) <= 64
      error_message = "IAM role name ${each.value} is longer than 64 characters; shorten var.name, var.environment or the project key."
    }
  }
}

# rds-db:connect for the IAM database users of the project's connections: the upstream aurora
# module builds the policy (one per user, scoped to its dbuser ARN); the job role only gets it.
resource "aws_iam_role_policy_attachment" "job_iam_database" {
  for_each = local.iam_database_connections

  role       = aws_iam_role.job[each.value.project].name
  policy_arn = each.value.policy_arn
}

resource "aws_iam_role_policy" "job" {
  for_each = local.job_role_names

  name   = "job-${each.key}"
  role   = aws_iam_role.job[each.key].id
  policy = jsonencode(local.job_role_policies[each.key])
}
