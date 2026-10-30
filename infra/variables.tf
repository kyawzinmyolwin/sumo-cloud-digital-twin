# Input variables. Defaults are chosen for this project; override per-environment
# values in terraform.tfvars (see terraform.tfvars.example). Never put secrets
# here — the InfluxDB token goes into Secrets Manager by hand (see README).

variable "aws_region" {
  description = "AWS region. ap-southeast-2 (Sydney) is the closest region to NZ."
  type        = string
  default     = "ap-southeast-2"
}

variable "project" {
  description = "Project name, used as a prefix for resource names and tags."
  type        = string
  default     = "christchurch-digital-twin"
}

variable "environment" {
  description = "Deployment environment (prod, dev, ...). Part of resource names."
  type        = string
  default     = "prod"
}

variable "dashboard_bucket_name" {
  description = <<-EOT
    Globally-unique S3 bucket name to host the static dashboard. S3 bucket names
    share one global namespace, so this cannot have a sensible default — pick
    something unique, e.g. "christchurch-digital-twin-dashboard-<your-initials>".
  EOT
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the Lambda log groups (Phase 2b)."
  type        = number
  default     = 14
}

variable "billing_alarm_threshold_usd" {
  description = "Estimated-charges alarm threshold in USD. Keep this low for a student project."
  type        = number
  default     = 5
}

variable "alarm_email" {
  description = "Email address subscribed to the billing alarm SNS topic. You must confirm the subscription from your inbox after apply."
  type        = string
}

# --- InfluxDB Cloud (used by storage.tf; fill in once you have an account) ------

variable "influxdb_url" {
  description = "InfluxDB Cloud regional URL, e.g. https://us-east-1-1.aws.cloud2.influxdata.com"
  type        = string
  default     = ""
}

variable "influxdb_org" {
  description = "InfluxDB Cloud organization name."
  type        = string
  default     = ""
}

variable "influxdb_bucket_name" {
  description = "InfluxDB bucket for tick metrics."
  type        = string
  default     = "traffic-metrics"
}

variable "influxdb_retention_seconds" {
  description = "Retention period for the InfluxDB bucket in seconds (0 = infinite; free tier max is 30 days = 2592000)."
  type        = number
  default     = 2592000
}

variable "sim_host_enabled" {
  description = "Create the on-demand EC2 SUMO producer. Keep false normally; set true only for a demo."
  type        = bool
  default     = false

}

variable "sim_host_instance_type" {
  description = "EC2 instance type for the on-demand SUMO producer. Keep small for a demo."
  type        = string
  default     = "t3.medium"
}

variable "sim_host_git_ref" {
  description = <<-EOT
    Git ref (branch, tag or commit) the sim host checks out when it boots. Defaults
    to the repo default branch (main). Set to a feature branch to demo work that
    isn't merged yet, e.g. -var 'sim_host_git_ref=claude/next-step-nun1nw'. Changing
    this replaces the instance (user_data change), so it re-clones the chosen ref.
  EOT
  type        = string
  default     = "main"
}

# --- ETL: CCC traffic-count workbooks (etl.tf) --------------------------------
variable "etl_data_bucket_name" {
  description = <<-EOT
    S3 bucket for ETL data (raw/processed/rejected workbooks + the manifest).
    Bucket names are one global namespace, so this has no default — pick a unique
    lowercase-hyphen name (e.g. christchurch-twin-traffic-data-<you>).
  EOT
  type        = string
}

variable "drive_folder_id" {
  description = "Top Google Drive folder id for the public CCC intersection counts."
  type        = string
  default     = "1oP5gcuKR1bHB9Xn2B4ILZZd_LhpMggxU"
}

variable "etl_schedule_expression" {
  description = "EventBridge schedule for the ingest Lambda (rate() or cron())."
  type        = string
  default     = "rate(7 days)"
}

variable "etl_max_files" {
  description = <<-EOT
    Optional cap on files handled per ingest run (empty string = no cap). Set a
    number for a first bounded backfill; the manifest lets repeated runs resume.
  EOT
  type        = string
  default     = ""
}