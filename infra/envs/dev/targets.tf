resource "aws_dynamodb_table" "targets" {
  name         = "uptime-targets-dev"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "url"

  attribute {
    name = "url"
    type = "S"
  }
}