############################################
# Fargate sql-runner: sandbox only, off by default (var.sql_runner = false), never part of the module.
#
# The module never builds an image (ADR 0011): SQL jobs run in Lambda, long jobs on SSM, and
# Fargate is an optional third runtime that takes a pre-pushed image URI. This file is a
# convenience for exercising that optional runtime here, because the dev host has no docker:
#
#   1. terraform apply -var sql_runner=true            ECR repository + CodeBuild project
#   2. sbx build-image <tag>                           CodeBuild builds and pushes, ECR scans
#   3. terraform apply -var sql_runner=true -var sql_runner_image_tag=<tag>
#                                                      task definition + ECR endpoints
#   4. sbx sync-jobs-fargate; e2e s1f                  reset and post jobs on Fargate
#
# CodeBuild builds from the same hardened Dockerfile as CI (digest-pinned base, checksums,
# hashed pip). Everything here goes with `terraform destroy` (force_delete on the repository).
############################################

locals {
  sql_runner_repo  = "${var.name}-sql-runner"
  sql_runner_image = var.sql_runner && var.sql_runner_image_tag != "" ? "${aws_ecr_repository.sql_runner[0].repository_url}:${var.sql_runner_image_tag}" : ""
  repo_root        = "${path.module}/../.."
  # The build context (repository root): only what the Dockerfile copies.
  sql_runner_files = concat(
    [
      "modules/dms-orchestration/src/jobs/sql-runner/Dockerfile",
      "modules/dms-orchestration/src/jobs/sql-runner/requirements.txt",
      "modules/dms-orchestration/src/jobs/sql-runner/orch_job.py",
      "modules/dms-orchestration/src/jobs/sql-runner/orch_sql.py",
      "modules/dms-orchestration/src/jobs/sql-runner/orch_dump.py",
      "modules/dms-orchestration/src/jobs/sql-runner/orch_verify.py",
      "modules/dms-orchestration/src/shared/__init__.py",
      "modules/dms-orchestration/src/shared/expr.py",
      "modules/dms-orchestration/src/shared/preflight.py",
      "modules/dms-orchestration/src/shared/fingerprint.py",
      "modules/dms-orchestration/src/shared/bundle.py",
      "modules/dms-orchestration/src/shared/errors.py",
    ],
    [for f in fileset("${local.repo_root}/modules/dms-orchestration/src/jobs/sql-runner/bin", "*") : "modules/dms-orchestration/src/jobs/sql-runner/bin/${f}"],
  )
}

resource "aws_ecr_repository" "sql_runner" {
  count                = var.sql_runner ? 1 : 0
  name                 = local.sql_runner_repo
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # sandbox: destroy removes the images too

  image_scanning_configuration {
    scan_on_push = true
  }
}

data "archive_file" "sql_runner_src" {
  count       = var.sql_runner ? 1 : 0
  type        = "zip"
  output_path = "${path.module}/.build/sql-runner-src.zip"

  dynamic "source" {
    for_each = toset(local.sql_runner_files)
    content {
      filename = source.value
      content  = file("${local.repo_root}/${source.value}")
    }
  }

  source {
    filename = "buildspec.yml"
    content  = <<-YAML
      version: 0.2
      phases:
        pre_build:
          commands:
            - test -n "$IMAGE_TAG"
            - aws ecr get-login-password | docker login --username AWS --password-stdin "$${REPO_URL%%/*}"
            # Docker Hub rate-limits CodeBuild's shared IPs: take the same official image (the
            # digest-pinned default of BASE_IMAGE) from the ECR Public mirror, which keeps the digest.
            - base=$(sed -n 's/^ARG BASE_IMAGE=//p' modules/dms-orchestration/src/jobs/sql-runner/Dockerfile | head -1)
            - test -n "$base"
        build:
          commands:
            - docker build -f modules/dms-orchestration/src/jobs/sql-runner/Dockerfile --build-arg BASE_IMAGE="public.ecr.aws/docker/library/$base" -t "$REPO_URL:$IMAGE_TAG" .
            # The CI gate (ci.yaml, trivy image): no fixable HIGH/CRITICAL, or no push.
            - docker run --rm -v /var/run/docker.sock:/var/run/docker.sock "$TRIVY_IMAGE" image --quiet --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$REPO_URL:$IMAGE_TAG"
        post_build:
          commands:
            - test "$CODEBUILD_BUILD_SUCCEEDING" = 1 && docker push "$REPO_URL:$IMAGE_TAG"
    YAML
  }
}

resource "aws_s3_object" "sql_runner_src" {
  count  = var.sql_runner ? 1 : 0
  bucket = module.dms_orchestration.artifacts_bucket
  key    = "codebuild/sql-runner-src.zip"
  source = data.archive_file.sql_runner_src[0].output_path
  etag   = data.archive_file.sql_runner_src[0].output_md5
}

resource "aws_cloudwatch_log_group" "codebuild" {
  count             = var.sql_runner ? 1 : 0
  name              = "/aws/codebuild/${local.sql_runner_repo}"
  retention_in_days = 1
}

resource "aws_iam_role" "codebuild" {
  count = var.sql_runner ? 1 : 0
  name  = "${var.name}-codebuild"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "codebuild" {
  count = var.sql_runner ? 1 : 0
  name  = "build-sql-runner"
  role  = aws_iam_role.codebuild[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.codebuild[0].arn}:*" },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "arn:aws:s3:::${module.dms_orchestration.artifacts_bucket}/codebuild/*" },
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      {
        Effect = "Allow"
        Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = aws_ecr_repository.sql_runner[0].arn
      },
    ]
  })
}

resource "aws_codebuild_project" "sql_runner" {
  count         = var.sql_runner ? 1 : 0
  name          = local.sql_runner_repo
  description   = "Sandbox: build the sql-runner image and push it to ECR"
  service_role  = aws_iam_role.codebuild[0].arn
  build_timeout = 30

  source {
    type     = "S3"
    location = "${module.dms_orchestration.artifacts_bucket}/${aws_s3_object.sql_runner_src[0].key}"
  }

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = true # docker build

    environment_variable {
      name  = "REPO_URL"
      value = aws_ecr_repository.sql_runner[0].repository_url
    }

    environment_variable {
      name  = "TRIVY_IMAGE"
      value = var.trivy_image
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild[0].name
    }
  }

  depends_on = [aws_iam_role_policy.codebuild]
}
