# ETL: CCC Miovision traffic-count workbooks -> S3 -> query API.
#
# etl-traffic-counts (scheduled): walks the public CCC Google Drive folder, downloads
#   new/updated workbooks, parses them (traffic_counts_parser), and writes raw +
#   cleaned JSON to S3; non-vehicle files are quarantined under rejected-.
# query-traffic-counts (HTTP API): GET /traffic-counts?intersection=&date= reads the
#   processed objects (keyed processed-traffic-data/<intersection>/<date>.json).
#
# PACKAGING: archive_file can't pip-install, so the bundles are staged by
#   functions/build_etl.py first:
#       python functions/build_etl.py && terraform apply
# The Drive API key is set by hand into the secret below (never in tfvars/state):
#       aws secretsmanager put-secret-value --secret-id <name> --secret-string 'KEY'

# --- data bucket (private) ----------------------------------------------------
resource "aws_s3_bucket" "etl_data" {
  bucket = var.etl_data_bucket_name
}

resource "aws_s3_bucket_ownership_controls" "etl_data" {
  bucket = aws_s3_bucket.etl_data.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "etl_data" {
  bucket                  = aws_s3_bucket.etl_data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- Drive API key secret (value set by hand) --------------------------------
resource "aws_secretsmanager_secret" "drive_api_key" {
  name        = "${local.name_prefix}-drive-api-key"
  description = "Google Drive API key used by etl-traffic-counts to read the public CCC counts folder."

  # 0 = no recovery window, so a student project can destroy/recreate without the
  # 7-day window blocking the next apply (see CLAUDE.md Secrets Manager gotcha).
  recovery_window_in_days = 0
}

# --- package the two functions (run functions/build_etl.py first) ------------
data "archive_file" "etl_ingest" {
  type        = "zip"
  source_dir  = "${path.module}/build/etl_ingest"
  output_path = "${path.module}/build/etl_ingest.zip"
}

data "archive_file" "etl_query" {
  type        = "zip"
  source_dir  = "${path.module}/build/etl_query"
  output_path = "${path.module}/build/etl_query.zip"
}

# --- IAM role shared by the two ETL Lambdas ----------------------------------
data "aws_iam_policy_document" "etl_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "etl" {
  name               = "${local.name_prefix}-etl-lambda"
  assume_role_policy = data.aws_iam_policy_document.etl_assume.json
}

data "aws_iam_policy_document" "etl_policy" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:*:*:*"]
  }
  statement {
    sid       = "DataBucket"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.etl_data.arn, "${aws_s3_bucket.etl_data.arn}/*"]
  }
  statement {
    sid       = "DriveKey"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.drive_api_key.arn]
  }
}

resource "aws_iam_role_policy" "etl" {
  name   = "${local.name_prefix}-etl-inline"
  role   = aws_iam_role.etl.id
  policy = data.aws_iam_policy_document.etl_policy.json
}

# --- ingest Lambda (scheduled) ------------------------------------------------
resource "aws_lambda_function" "etl_ingest" {
  function_name    = "${local.name_prefix}-etl-traffic-counts"
  role             = aws_iam_role.etl.arn
  runtime          = local.lambda_runtime
  handler          = "etl.lambda_ingest.lambda_handler"
  filename         = data.archive_file.etl_ingest.output_path
  source_code_hash = data.archive_file.etl_ingest.output_base64sha256
  timeout          = 300
  memory_size      = 512

  # NOTE: the ingest reads-modifies-writes one S3 manifest, so overlapping runs
  # race on it. The natural guard is reserved_concurrent_executions = 1, but that
  # requires the account's unreserved concurrency pool to stay >= 10, which a
  # low-limit account (total quota 10) can't satisfy — so it's intentionally NOT
  # set here. Instead, invoke sequentially and without CLI retries
  # (--cli-read-timeout 360, AWS_MAX_ATTEMPTS=1); the weekly schedule makes an
  # accidental overlap unlikely, and the manifest converges if one occurs.

  environment {
    variables = {
      DATA_BUCKET                   = aws_s3_bucket.etl_data.id
      DRIVE_SECRET_ARN              = aws_secretsmanager_secret.drive_api_key.arn
      DRIVE_FOLDER_ID               = var.drive_folder_id
      MAX_FILES                     = var.etl_max_files
      TRAFFIC_PARSER_NO_VENV_REEXEC = "1"
    }
  }
}

# --- query Lambda (HTTP API) --------------------------------------------------
resource "aws_lambda_function" "etl_query" {
  function_name    = "${local.name_prefix}-query-traffic-counts"
  role             = aws_iam_role.etl.arn
  runtime          = local.lambda_runtime
  handler          = "etl.lambda_query.lambda_handler"
  filename         = data.archive_file.etl_query.output_path
  source_code_hash = data.archive_file.etl_query.output_base64sha256
  timeout          = 20
  memory_size      = 256

  environment {
    variables = {
      DATA_BUCKET = aws_s3_bucket.etl_data.id
    }
  }
}

resource "aws_cloudwatch_log_group" "etl_ingest" {
  name              = "/aws/lambda/${aws_lambda_function.etl_ingest.function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "etl_query" {
  name              = "/aws/lambda/${aws_lambda_function.etl_query.function_name}"
  retention_in_days = var.log_retention_days
}

# --- EventBridge schedule -> ingest ------------------------------------------
resource "aws_cloudwatch_event_rule" "etl_schedule" {
  name                = "${local.name_prefix}-etl-schedule"
  description         = "Scheduled trigger for the CCC traffic-count ingest."
  schedule_expression = var.etl_schedule_expression
}

resource "aws_cloudwatch_event_target" "etl_schedule" {
  rule      = aws_cloudwatch_event_rule.etl_schedule.name
  target_id = "etl-traffic-counts"
  arn       = aws_lambda_function.etl_ingest.arn
}

resource "aws_lambda_permission" "etl_schedule" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.etl_ingest.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.etl_schedule.arn
}

# --- HTTP API in front of the query Lambda -----------------------------------
# Same shape as replay_api.tf: the Lambda returns the {statusCode,headers,body}
# payload-v2.0 response and sets its own Access-Control-Allow-Origin, so we do
# NOT add API-level CORS (which would duplicate the header and break the browser).
resource "aws_apigatewayv2_api" "etl_query" {
  name          = "${local.name_prefix}-counts-http"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "etl_query" {
  api_id                 = aws_apigatewayv2_api.etl_query.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.etl_query.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "etl_query" {
  api_id    = aws_apigatewayv2_api.etl_query.id
  route_key = "GET /traffic-counts"
  target    = "integrations/${aws_apigatewayv2_integration.etl_query.id}"
}

resource "aws_apigatewayv2_stage" "etl_query" {
  api_id      = aws_apigatewayv2_api.etl_query.id
  name        = "prod"
  auto_deploy = true
}

resource "aws_lambda_permission" "etl_query_apigw" {
  statement_id  = "AllowCountsHttpApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.etl_query.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.etl_query.execution_arn}/*/*"
}

# --- outputs ------------------------------------------------------------------
output "counts_api_url" {
  description = "Public counts endpoint. GET {this}?intersection=I0007&date=2016-08-24"
  value       = "${aws_apigatewayv2_stage.etl_query.invoke_url}/traffic-counts"
}

output "etl_data_bucket" {
  description = "S3 bucket holding raw/processed/rejected traffic-count data + the manifest."
  value       = aws_s3_bucket.etl_data.id
}

output "drive_api_key_secret_name" {
  description = "Put the Drive API key here: aws secretsmanager put-secret-value --secret-id <this> --secret-string 'KEY'"
  value       = aws_secretsmanager_secret.drive_api_key.name
}
