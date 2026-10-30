# Outputs surfaced after `terraform apply`. These grow as we add the API Gateway
# and CloudFront layers (websocket_url, dashboard_url will land then).

output "dashboard_bucket_name" {
  description = "S3 bucket hosting the dashboard."
  value       = aws_s3_bucket.dashboard.id
}

output "dashboard_bucket_arn" {
  description = "ARN of the dashboard bucket (referenced by the CloudFront OAC policy)."
  value       = aws_s3_bucket.dashboard.arn
}

output "influxdb_token_secret_name" {
  description = "Secrets Manager secret name to populate with the InfluxDB token (put-secret-value)."
  value       = aws_secretsmanager_secret.influxdb_token.name
}

output "influxdb_token_secret_arn" {
  description = "ARN of the InfluxDB token secret (granted to the metrics Lambda)."
  value       = aws_secretsmanager_secret.influxdb_token.arn
}

output "websocket_url" {
  description = "Public WebSocket endpoint — the emitter sends here, browsers connect here."
  value       = aws_apigatewayv2_stage.prod.invoke_url
}

output "replay_url" {
  description = "HTTPS endpoint for historical metric reads (the dashboard's replay/history calls)."
  value       = aws_lambda_function_url.replay.function_url
}

output "dashboard_url" {
  description = "CloudFront URL serving the dashboard over HTTPS."
  value       = "https://${aws_cloudfront_distribution.dashboard.domain_name}"
}

output "sim_host_instance_id" {
  description = "SUMO producer instance id (for: aws ssm start-session --target <id>). Null when disabled."
  value       = try(aws_instance.sim_host[0].id, null)
}

output "sim_host_public_ip" {
  description = "SUMO producer public IP (informational; access is via SSM)."
  value       = try(aws_instance.sim_host[0].public_ip, null)
}