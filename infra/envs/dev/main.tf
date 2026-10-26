terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {}
}

provider "aws" {
  region = "us-east-1"
}

resource "aws_dynamodb_table" "checks" {
  name         = "uptime-checks-dev"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "url"
  range_key    = "timestamp"

  attribute {
    name = "url"
    type = "S"
  }
  attribute {
    name = "timestamp"
    type = "S"
  }
}

resource "aws_iam_role" "checker" {
  name = "uptime-checker-dev"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.checker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "archive_file" "checker" {
  type        = "zip"
  source_file = "${path.module}/../../../src/checker.py"
  output_path = "${path.module}/checker.zip"
}

resource "aws_lambda_function" "checker" {
  function_name    = "uptime-checker-dev"
  role             = aws_iam_role.checker.arn
  handler          = "checker.handler"
  runtime          = "python3.13"
  filename         = data.archive_file.checker.output_path
  source_code_hash = data.archive_file.checker.output_base64sha256
  timeout          = 15
}