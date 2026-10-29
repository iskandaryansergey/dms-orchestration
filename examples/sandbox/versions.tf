terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.90.0, < 6.0.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

# Credentials: AWS_PROFILE (e.g. <profile>). Local state only; destroy after every run.
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = var.project_tag
      managed-by = "terraform"
      example    = "dms-orchestration/examples/sandbox"
    }
  }
}
