# Lambda functions + IAM + the connections table.
#
# Three functions, one shared execution role (right-sized for this project):
#   traffic-ingest   — WebSocket $connect/$disconnect/sendmessage; records browser
#                      connections, fans snapshots out to them, and async-invokes
#                      traffic-metrics.
#   traffic-metrics  — computes compute_tick_metrics() and writes to InfluxDB Cloud.
#   traffic-replay   — HTTP (Function URL): queries InfluxDB for a time range.
#
# The metrics/replay handlers use only the Python stdlib (urllib) to talk to
# InfluxDB, so there is nothing to vendor — the function directory zips as-is.

locals {
  lambda_runtime = "python3.12"
  name_prefix    = "${var.project}-${var.environment}"
}

# --- Connection registry (who is listening) ----------------------------------
resource "aws_dynamodb_table" "connections" {
  name         = "${local.name_prefix}-connections"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "connectionId"

  attribute {
    name = "connectionId"
    type = "S"
  }

  ttl {
    attribute_name = "expireAt"
    enabled        = true
  }
}

# --- Package each function directory ------------------------------------------
data "archive_file" "ingest" {
  type        = "zip"
  source_dir  = "${path.module}/functions/traffic_ingest"
  output_path = "${path.module}/build/traffic_ingest.zip"
}

data "archive_file" "metrics" {
  type        = "zip"
  source_dir  = "${path.module}/functions/traffic_metrics"
  output_path = "${path.module}/build/traffic_metrics.zip"
}

data "archive_file" "replay" {
  type        = "zip"
  source_dir  = "${path.module}/functions/traffic_replay"
  output_path = "${path.module}/build/traffic_replay.zip"
}

# --- Execution role -----------------------------------------------------------
resource "aws_iam_role" "lambda" {
  name = "${local.name_prefix}-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# CloudWatch Logs (create group/stream, put events).
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Everything else the functions need, least-privilege to our own resources.
resource "aws_iam_role_policy" "lambda_inline" {
  name = "${local.name_prefix}-lambda-inline"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "PushToConnectedClients"
        Effect   = "Allow"
        Action   = "execute-api:ManageConnections"
        Resource = "${aws_apigatewayv2_api.ws.execution_arn}/*"
      },
      {
        Sid      = "ConnectionRegistry"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Scan", "dynamodb:GetItem"]
        Resource = aws_dynamodb_table.connections.arn
      },
      {
        Sid      = "InvokeMetrics"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.metrics.arn
      },
      {
        Sid      = "ReadInfluxToken"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.influxdb_token.arn
      }
    ]
  })
}

# --- Functions ----------------------------------------------------------------
resource "aws_lambda_function" "ingest" {
  function_name    = "${local.name_prefix}-traffic-ingest"
  role             = aws_iam_role.lambda.arn
  runtime          = local.lambda_runtime
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.ingest.output_path
  source_code_hash = data.archive_file.ingest.output_base64sha256
  timeout          = 15
  memory_size      = 128

  environment {
    variables = {
      CONNECTIONS_TABLE = aws_dynamodb_table.connections.name
      METRICS_FUNCTION  = aws_lambda_function.metrics.function_name
    }
  }
}

resource "aws_lambda_function" "metrics" {
  function_name    = "${local.name_prefix}-traffic-metrics"
  role             = aws_iam_role.lambda.arn
  runtime          = local.lambda_runtime
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.metrics.output_path
  source_code_hash = data.archive_file.metrics.output_base64sha256
  timeout          = 15
  memory_size      = 128

  environment {
    variables = {
      INFLUXDB_URL        = var.influxdb_url
      INFLUXDB_ORG        = var.influxdb_org
      INFLUXDB_BUCKET     = var.influxdb_bucket_name
      INFLUXDB_SECRET_ARN = aws_secretsmanager_secret.influxdb_token.arn
    }
  }
}

resource "aws_lambda_function" "replay" {
  function_name    = "${local.name_prefix}-traffic-replay"
  role             = aws_iam_role.lambda.arn
  runtime          = local.lambda_runtime
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.replay.output_path
  source_code_hash = data.archive_file.replay.output_base64sha256
  timeout          = 20
  memory_size      = 128

  environment {
    variables = {
      INFLUXDB_URL        = var.influxdb_url
      INFLUXDB_ORG        = var.influxdb_org
      INFLUXDB_BUCKET     = var.influxdb_bucket_name
      INFLUXDB_SECRET_ARN = aws_secretsmanager_secret.influxdb_token.arn
    }
  }
}

# Public HTTPS endpoint for the replay reads (the dashboard calls this). CORS is
# open here for simplicity; tighten allow_origins to the CloudFront domain later.
resource "aws_lambda_function_url" "replay" {
  function_name      = aws_lambda_function.replay.function_name
  authorization_type = "NONE"

  cors {
    allow_origins = ["*"]
    allow_methods = ["GET"]
    allow_headers = ["content-type"]
    max_age       = 3600
  }
}

# A "NONE" auth Function URL still needs an explicit resource-based permission
# granting public invoke — the console adds this automatically, Terraform does not.
# Without it the URL returns 403 Forbidden.
resource "aws_lambda_permission" "replay_url_public" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.replay.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}
