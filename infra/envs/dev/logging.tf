resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/uptime-api-dev"
  retention_in_days = 14
}

resource "aws_iam_role" "apigw_logs" {
  name = "uptime-apigw-logs-dev"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "apigw_logs" {
  role       = aws_iam_role.apigw_logs.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "api" {
  cloudwatch_role_arn = aws_iam_role.apigw_logs.arn
  depends_on          = [aws_iam_role_policy_attachment.apigw_logs]
}

resource "aws_api_gateway_method_settings" "api" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  stage_name  = aws_api_gateway_stage.dev.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled = true
    logging_level   = "ERROR"
  }
}