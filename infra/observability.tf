# Observability: one log group per Lambda (with retention) + a billing alarm.

# --- Log groups ---------------------------------------------------------------
# Declaring these explicitly (instead of letting Lambda auto-create them) is the
# only way to set a retention period, so logs don't accumulate cost forever.
resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${aws_lambda_function.ingest.function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "metrics" {
  name              = "/aws/lambda/${aws_lambda_function.metrics.function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "replay" {
  name              = "/aws/lambda/${aws_lambda_function.replay.function_name}"
  retention_in_days = var.log_retention_days
}

# --- Billing alarm (us-east-1) ------------------------------------------------
# The safety net: email you if estimated charges cross the threshold.
resource "aws_sns_topic" "billing" {
  provider = aws.us_east_1
  name     = "${local.name_prefix}-billing-alerts"
}

resource "aws_sns_topic_subscription" "billing_email" {
  provider  = aws.us_east_1
  topic_arn = aws_sns_topic.billing.arn
  protocol  = "email"
  endpoint  = var.alarm_email
  # You must click the confirmation link AWS emails you, or the alarm can't notify.
}

resource "aws_cloudwatch_metric_alarm" "billing" {
  provider            = aws.us_east_1
  alarm_name          = "${local.name_prefix}-estimated-charges"
  alarm_description   = "Estimated AWS charges exceeded $${var.billing_alarm_threshold_usd}."
  namespace           = "AWS/Billing"
  metric_name         = "EstimatedCharges"
  statistic           = "Maximum"
  dimensions          = { Currency = "USD" }
  period              = 21600 # 6h — billing metrics update slowly
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.billing_alarm_threshold_usd
  alarm_actions       = [aws_sns_topic.billing.arn]
}
