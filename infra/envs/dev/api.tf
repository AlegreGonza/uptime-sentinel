data "archive_file" "api" {
  type        = "zip"
  source_file = "${path.module}/../../../src/api.py"
  output_path = "api.zip"
}

resource "aws_iam_role" "api" {
  name = "uptime-api-dev"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "api_logs" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "api" {
  name = "uptime-api-dev"
  role = aws_iam_role.api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Scan"]
        Resource = aws_dynamodb_table.targets.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:Query"]
        Resource = aws_dynamodb_table.checks.arn
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.checker.arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/uptime-api-dev"
  retention_in_days = 14
}

resource "aws_lambda_function" "api" {
  function_name    = "uptime-api-dev"
  role             = aws_iam_role.api.arn
  handler          = "api.handler"
  runtime          = "python3.13"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  timeout          = 20
  memory_size      = 128

  environment {
    variables = {
      CHECKER_FUNCTION = aws_lambda_function.checker.function_name
      TARGETS_TABLE    = aws_dynamodb_table.targets.name
      RESULTS_TABLE    = aws_dynamodb_table.checks.name
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.api_logs,
    aws_cloudwatch_log_group.api,
  ]
}

resource "aws_api_gateway_rest_api" "api" {
  name = "uptime-api-dev"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_resource" "probar" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "probar"
}

resource "aws_api_gateway_resource" "objetivos" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "objetivos"
}

resource "aws_api_gateway_resource" "resultados" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "resultados"
}

resource "aws_api_gateway_method" "probar_post" {
  rest_api_id      = aws_api_gateway_rest_api.api.id
  resource_id      = aws_api_gateway_resource.probar.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_method" "objetivos_post" {
  rest_api_id      = aws_api_gateway_rest_api.api.id
  resource_id      = aws_api_gateway_resource.objetivos.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_method" "objetivos_get" {
  rest_api_id      = aws_api_gateway_rest_api.api.id
  resource_id      = aws_api_gateway_resource.objetivos.id
  http_method      = "GET"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_method" "resultados_get" {
  rest_api_id      = aws_api_gateway_rest_api.api.id
  resource_id      = aws_api_gateway_resource.resultados.id
  http_method      = "GET"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_integration" "probar_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.probar.id
  http_method             = aws_api_gateway_method.probar_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_api_gateway_integration" "objetivos_post" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.objetivos.id
  http_method             = aws_api_gateway_method.objetivos_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_api_gateway_integration" "objetivos_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.objetivos.id
  http_method             = aws_api_gateway_method.objetivos_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_api_gateway_integration" "resultados_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.resultados.id
  http_method             = aws_api_gateway_method.resultados_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_lambda_permission" "allow_apigateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "api" {
  rest_api_id = aws_api_gateway_rest_api.api.id

  triggers = {
    redeploy = sha1(jsonencode([
      aws_api_gateway_integration.probar_post.id,
      aws_api_gateway_integration.objetivos_post.id,
      aws_api_gateway_integration.objetivos_get.id,
      aws_api_gateway_integration.resultados_get.id,
      data.archive_file.api.output_base64sha256,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "dev" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  deployment_id = aws_api_gateway_deployment.api.id
  stage_name    = var.env

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      responseLength = "$context.responseLength"
    })
  }

  depends_on = [aws_api_gateway_account.api]
}

resource "aws_api_gateway_api_key" "api" {
  name    = "uptime-api-key-dev"
  enabled = true
}

resource "aws_api_gateway_usage_plan" "api" {
  name = "uptime-plan-dev"

  api_stages {
    api_id = aws_api_gateway_rest_api.api.id
    stage  = aws_api_gateway_stage.dev.stage_name
  }

  throttle_settings {
    rate_limit  = 5
    burst_limit = 10
  }

  quota_settings {
    limit  = 1000
    period = "DAY"
  }
}

resource "aws_api_gateway_usage_plan_key" "api" {
  key_id        = aws_api_gateway_api_key.api.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.api.id
}

output "api_url" {
  value = aws_api_gateway_stage.dev.invoke_url
}

output "api_key_id" {
  value = aws_api_gateway_api_key.api.id
}