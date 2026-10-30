# Public HTTPS endpoint for the replay reads (historical charts), fronting the
# traffic-replay Lambda with an API Gateway HTTP API.
#
# Why not the Lambda Function URL? On this account anonymous Function URL access
# is 403-blocked (see storage.tf/README notes). An HTTP API is not subject to
# that restriction, so this is what the DEPLOYED dashboard's History panel calls:
#   https://<id>.execute-api.<region>.amazonaws.com/prod/metrics
#
# No Lambda change needed — the traffic-replay handler already returns the
# {statusCode, headers, body} shape (HTTP API payload v2.0) and sets an
# Access-Control-Allow-Origin header. The dashboard's fetch is a simple GET (no
# custom headers), so no CORS preflight is involved and the Lambda's own header
# is enough — we deliberately don't add API-level CORS (which would duplicate the
# Access-Control-Allow-Origin header and break the browser).

resource "aws_apigatewayv2_api" "replay" {
  name          = "${local.name_prefix}-replay-http"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "replay" {
  api_id                 = aws_apigatewayv2_api.replay.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.replay.invoke_arn
  integration_method     = "POST" # Lambda proxy is always POST under the hood
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "replay" {
  api_id    = aws_apigatewayv2_api.replay.id
  route_key = "GET /metrics"
  target    = "integrations/${aws_apigatewayv2_integration.replay.id}"
}

resource "aws_apigatewayv2_stage" "replay" {
  api_id      = aws_apigatewayv2_api.replay.id
  name        = "prod"
  auto_deploy = true
}

# Allow this HTTP API to invoke the replay Lambda.
resource "aws_lambda_permission" "replay_apigw" {
  statement_id  = "AllowReplayHttpApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.replay.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.replay.execution_arn}/*/*"
}

output "replay_api_url" {
  description = "Public HTTPS replay endpoint for the dashboard History panel (use as ?replay=<this>)."
  value       = "${aws_apigatewayv2_stage.replay.invoke_url}/metrics"
}
