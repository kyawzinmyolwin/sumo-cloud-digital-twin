# Authenticated cloud scenario-control API.
#
# Lets the HOSTED dashboard start/stop named-preset simulation runs on the EC2 sim
# host, mirroring control_server.py's HTTP contract so the same dashboard panel
# drives either local or cloud. The traffic-control Lambda rewrites the sim host's
# sumo-emitter scenario args via SSM Run Command and restarts the service.
#
# SAFETY: the Lambda only composes commands from a fixed preset allowlist (+ a
# regex-validated edge id); it never runs arbitrary input. The endpoint is gated by
# a shared token (a Lambda authorizer) because starting a run triggers compute/cost.
#
# Flow:
#   dashboard --POST /start (Authorization: Bearer <token>)--> HTTP API
#     -> control-authorizer (token check) -> traffic-control Lambda
#     -> ssm:SendCommand to the sim host (tag:Name) -> systemctl restart sumo-emitter
#
# After apply:  terraform output control_api_url ; terraform output -raw control_api_token
# Point the dashboard at it with ?control=<control_api_url>&token=<control_api_token>.

resource "random_password" "control_token" {
  length  = 40
  special = false
}

data "archive_file" "control" {
  type        = "zip"
  source_dir  = "${path.module}/functions/traffic_control"
  output_path = "${path.module}/build/traffic_control.zip"
}

data "archive_file" "control_authorizer" {
  type        = "zip"
  source_dir  = "${path.module}/functions/control_authorizer"
  output_path = "${path.module}/build/control_authorizer.zip"
}

# --- Roles (least-privilege, separate from the data-plane Lambda role) --------
resource "aws_iam_role" "control" {
  name = "${local.name_prefix}-control"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "control_logs" {
  role       = aws_iam_role.control.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "control_inline" {
  name = "${local.name_prefix}-control-inline"
  role = aws_iam_role.control.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # The RunShellScript document itself carries no tag, so it gets an
        # unconditioned allow (a tag condition here would deny it — the bug this
        # fixes). Safe: the document can only run against instances also allowed below.
        Sid      = "UseRunShellScriptDocument"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = "arn:aws:ssm:${var.aws_region}::document/AWS-RunShellScript"
      },
      {
        # Restrict the instance target to our sim host by tag.
        Sid      = "SendCommandToSimHostOnly"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = "arn:aws:ec2:${var.aws_region}:*:instance/*"
        Condition = { StringEquals = { "ssm:resourceTag/Name" = "${local.name_prefix}-sim-host" } }
      },
      {
        Sid      = "DescribeSimHost"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Sid      = "ControlState"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/${var.project}/${var.environment}/sim-control/*"
      }
    ]
  })
}

resource "aws_iam_role" "control_authorizer" {
  name = "${local.name_prefix}-control-authorizer"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "control_authorizer_logs" {
  role       = aws_iam_role.control_authorizer.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# --- Functions ----------------------------------------------------------------
resource "aws_lambda_function" "control" {
  function_name    = "${local.name_prefix}-traffic-control"
  role             = aws_iam_role.control.arn
  runtime          = local.lambda_runtime
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.control.output_path
  source_code_hash = data.archive_file.control.output_base64sha256
  timeout          = 20
  memory_size      = 128

  environment {
    variables = {
      SIM_HOST_TAG = "${local.name_prefix}-sim-host"
      STATE_PARAM  = "/${var.project}/${var.environment}/sim-control/state"
    }
  }
}

resource "aws_lambda_function" "control_authorizer" {
  function_name    = "${local.name_prefix}-control-authorizer"
  role             = aws_iam_role.control_authorizer.arn
  runtime          = local.lambda_runtime
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.control_authorizer.output_path
  source_code_hash = data.archive_file.control_authorizer.output_base64sha256
  timeout          = 5
  memory_size      = 128

  environment {
    variables = { CONTROL_TOKEN = random_password.control_token.result }
  }
}

# --- HTTP API + token authorizer ----------------------------------------------
resource "aws_apigatewayv2_api" "control" {
  name          = "${local.name_prefix}-control-http"
  protocol_type = "HTTP"

  # The dashboard sends a custom Authorization header, so a CORS preflight applies.
  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["authorization", "content-type"]
    max_age       = 3600
  }
}

resource "aws_apigatewayv2_integration" "control" {
  api_id                 = aws_apigatewayv2_api.control.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.control.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_authorizer" "control" {
  api_id                            = aws_apigatewayv2_api.control.id
  authorizer_type                   = "REQUEST"
  authorizer_uri                    = aws_lambda_function.control_authorizer.invoke_arn
  identity_sources                  = ["$request.header.Authorization"]
  name                              = "${local.name_prefix}-control-token"
  authorizer_payload_format_version = "2.0"
  enable_simple_responses           = true
}

resource "aws_lambda_permission" "control_authorizer_invoke" {
  statement_id  = "AllowControlAuthorizerInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.control_authorizer.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.control.execution_arn}/*"
}

locals {
  control_routes = ["GET /scenarios", "GET /status", "POST /start", "POST /stop", "POST /start_roadblock"]
}

resource "aws_apigatewayv2_route" "control" {
  for_each           = toset(local.control_routes)
  api_id             = aws_apigatewayv2_api.control.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.control.id}"
  authorization_type = "CUSTOM"
  authorizer_id      = aws_apigatewayv2_authorizer.control.id
}

resource "aws_apigatewayv2_stage" "control" {
  api_id      = aws_apigatewayv2_api.control.id
  name        = "prod"
  auto_deploy = true
}

resource "aws_lambda_permission" "control_apigw" {
  statement_id  = "AllowControlHttpApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.control.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.control.execution_arn}/*/*"
}

# --- Outputs ------------------------------------------------------------------
output "control_api_url" {
  description = "Authenticated cloud control endpoint. Dashboard: ?control=<this>."
  value       = aws_apigatewayv2_stage.control.invoke_url
}

output "control_api_token" {
  description = "Shared token for the control API. Dashboard: ?token=<this>. Read with: terraform output -raw control_api_token"
  value       = random_password.control_token.result
  sensitive   = true
}
