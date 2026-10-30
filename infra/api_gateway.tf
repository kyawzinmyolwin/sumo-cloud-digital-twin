# WebSocket API Gateway — the public real-time endpoint.
#
# The SUMO emitter connects as a producer and sends vehicle snapshots on the
# "sendmessage" route; browsers connect to receive broadcasts. All routes are
# handled by the traffic-ingest Lambda (see lambda.tf).

resource "aws_apigatewayv2_api" "ws" {
  name                       = "${var.project}-${var.environment}-ws"
  protocol_type              = "WEBSOCKET"
  route_selection_expression = "$request.body.action"
}

# --- Integration: every route invokes traffic-ingest -------------------------
resource "aws_apigatewayv2_integration" "ingest" {
  api_id                    = aws_apigatewayv2_api.ws.id
  integration_type          = "AWS_PROXY"
  integration_uri           = aws_lambda_function.ingest.invoke_arn
  content_handling_strategy = "CONVERT_TO_TEXT"
  # WebSocket integrations must use POST regardless of the client action.
  integration_method = "POST"
}

# --- Routes ------------------------------------------------------------------
# $connect / $disconnect are lifecycle; sendmessage is the producer channel;
# $default catches anything else.
resource "aws_apigatewayv2_route" "connect" {
  api_id    = aws_apigatewayv2_api.ws.id
  route_key = "$connect"
  target    = "integrations/${aws_apigatewayv2_integration.ingest.id}"
}

resource "aws_apigatewayv2_route" "disconnect" {
  api_id    = aws_apigatewayv2_api.ws.id
  route_key = "$disconnect"
  target    = "integrations/${aws_apigatewayv2_integration.ingest.id}"
}

resource "aws_apigatewayv2_route" "sendmessage" {
  api_id    = aws_apigatewayv2_api.ws.id
  route_key = "sendmessage"
  target    = "integrations/${aws_apigatewayv2_integration.ingest.id}"
}

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.ws.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.ingest.id}"
}

# --- Stage (auto-deploy) -----------------------------------------------------
resource "aws_apigatewayv2_stage" "prod" {
  api_id      = aws_apigatewayv2_api.ws.id
  name        = var.environment
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 50
    throttling_rate_limit  = 50
  }
}

# Allow API Gateway to invoke the ingest Lambda.
resource "aws_lambda_permission" "apigw_ingest" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "apigateway.amazonaws.com"
  # Any route/stage of this API may invoke it.
  source_arn = "${aws_apigatewayv2_api.ws.execution_arn}/*/*"
}
