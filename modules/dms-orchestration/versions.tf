terraform {
  # Workspaces are pinned to 1.9.8. Do not use features from 1.10+ (ephemeral values, override_during).
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    # Intersection with the upstream modules: dms (>= 5.70, < 6) and aurora (>= 5.90, < 7).
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.90.0, < 6.0.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}
