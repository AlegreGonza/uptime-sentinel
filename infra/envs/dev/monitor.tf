resource "aws_iam_role" "monitor" {
  name = "uptime-monitor-dev"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "monitor_logs" {
  role       = aws_iam_role.monitor.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "monitor" {
  role = aws_iam_role.monitor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "dynamodb:Scan"
        Resource = aws_dynamodb_table.targets.arn
      },
      {
        Effect   = "Allow"
        Action   = "dynamodb:PutItem"
        Resource = aws_dynamodb_table.checks.arn
      },
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.checker.arn
      }
    ]
  })
}

data "archive_file" "monitor" {
  type        = "zip"
  source_file = "${path.module}/../../../src/monitor.py"
  output_path = "${path.module}/monitor.zip"
}

resource "aws_lambda_function" "monitor" {
  function_name    = "uptime-monitor-dev"
  role             = aws_iam_role.monitor.arn
  handler          = "monitor.handler"
  runtime          = "python3.13"
  filename         = data.archive_file.monitor.output_path
  source_code_hash = data.archive_file.monitor.output_base64sha256
  timeout          = 120

  environment {
    variables = {
      CHECKER_FUNCTION = aws_lambda_function.checker.function_name
      TARGETS_TABLE    = aws_dynamodb_table.targets.name
      RESULTS_TABLE    = aws_dynamodb_table.checks.name
      ALERTS_TOPIC     = aws_sns_topic.alerts.arn
    }
  }
}

resource "aws_cloudwatch_log_group" "monitor" {
  name              = "/aws/lambda/uptime-monitor-dev"
  retention_in_days = 14
}