resource "aws_cloudwatch_event_rule" "monitor_schedule" {
  name                = "uptime-monitor-schedule-dev"
  schedule_expression = "rate(5 minutes)"
  state               = "DISABLED"
}

resource "aws_cloudwatch_event_target" "monitor" {
  rule = aws_cloudwatch_event_rule.monitor_schedule.name
  arn  = aws_lambda_function.monitor.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.monitor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.monitor_schedule.arn
}