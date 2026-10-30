# Terraform + provider version pins.
#
# Pinning avoids "it worked last week" drift: a fresh `terraform init` on any
# machine resolves the same provider versions. Bump these deliberately, not by
# accident.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60" # 5.x, at least 5.60
    }

    # Used later by storage.tf to create the InfluxDB Cloud bucket. Harmless to
    # declare now; it is only initialised if a resource/provider block uses it.
    influxdb = {
      source  = "komminarlabs/influxdb"
      version = "~> 1.3"
    }

    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }

    # Zips the Lambda source directories at plan time.
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}
