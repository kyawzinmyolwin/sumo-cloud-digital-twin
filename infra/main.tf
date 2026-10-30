# Provider + remote state configuration.
#
# WHY A REMOTE BACKEND?
# Terraform records what it created in a "state" file. Keeping that state in S3
# (with a DynamoDB lock) instead of on your laptop means it survives a lost
# machine and prevents two applies from clobbering each other. The state bucket
# and lock table must already exist BEFORE `terraform init` — see infra/README.md
# (they are created by hand once; Terraform cannot bootstrap its own backend).
#
# The backend block is intentionally EMPTY here (partial configuration). Real
# values come from backend.hcl at init time:
#     terraform init -backend-config=backend.hcl
# That keeps account-specific bucket names out of version control.

terraform {
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  # Tags applied to every taggable resource — makes cost/ownership legible in
  # the AWS console and billing reports.
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repo        = "sumo-cloud-digital-twin"
    }
  }
}

# AWS publishes billing metrics (AWS/Billing EstimatedCharges) only in us-east-1,
# so the billing alarm + its SNS topic must live there — this aliased provider is
# used only by those resources in observability.tf.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repo        = "sumo-cloud-digital-twin"
    }
  }
}
