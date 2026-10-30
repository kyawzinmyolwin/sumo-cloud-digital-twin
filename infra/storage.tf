# Storage layer: the S3 bucket that serves the dashboard, and the Secrets Manager
# secret that holds the InfluxDB token. (CloudFront that fronts this bucket is in
# cdn.tf; the InfluxDB bucket itself is created in the InfluxDB Cloud UI for now —
# see the note at the bottom.)

# --- Dashboard hosting bucket -------------------------------------------------
# Private bucket. It is NOT a public "website" bucket — CloudFront reads from it
# via Origin Access Control (wired up in cdn.tf), so the bucket stays locked down
# and only the CDN can serve its objects.

resource "aws_s3_bucket" "dashboard" {
  bucket = var.dashboard_bucket_name
}

# Disable ACLs entirely (modern best practice). Ownership is enforced to the
# bucket owner; access is granted only through bucket policy.
resource "aws_s3_bucket_ownership_controls" "dashboard" {
  bucket = aws_s3_bucket.dashboard.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Block every form of public access. CloudFront reaches the objects through a
# signed OAC request, not through public ACLs or policies.
resource "aws_s3_bucket_public_access_block" "dashboard" {
  bucket                  = aws_s3_bucket.dashboard.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Keep old versions of dashboard files so a bad deploy can be rolled back.
resource "aws_s3_bucket_versioning" "dashboard" {
  bucket = aws_s3_bucket.dashboard.id
  versioning_configuration {
    status = "Enabled"
  }
}

# --- InfluxDB token secret ----------------------------------------------------
# This creates the SECRET CONTAINER only. Its value (the actual token) is set by
# hand, once, so the token never lands in Terraform state or the repo:
#   aws secretsmanager put-secret-value \
#     --secret-id <this secret's name> --secret-string 'YOUR_INFLUX_TOKEN'
# The traffic-metrics Lambda reads it at runtime (wired up in lambda.tf).

resource "aws_secretsmanager_secret" "influxdb_token" {
  name        = "${var.project}-${var.environment}-influxdb-token"
  description = "InfluxDB Cloud API token used by the traffic-metrics Lambda to write tick metrics."

  # Short recovery window so a student project can be torn down/recreated quickly.
  recovery_window_in_days = 7
}

# NOTE — InfluxDB bucket:
# CLAUDE.md lists influxdb_bucket as a Terraform resource via the InfluxDB
# provider. For now, create the bucket in the InfluxDB Cloud UI (setup order
# step 4) and set influxdb_bucket_name in tfvars. We will add the provider-managed
# resource once its exact schema is confirmed against the provider docs, rather
# than guess it here. The versions.tf declaration for the influxdb provider is
# already in place for that step.
