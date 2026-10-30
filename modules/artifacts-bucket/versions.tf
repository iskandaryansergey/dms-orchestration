terraform {
  # Workspaces are pinned to 1.9.8. Do not use features from 1.10+.
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.90.0, < 6.0.0"
    }
  }
}
