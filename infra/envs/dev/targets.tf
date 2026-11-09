resource "aws_dynamodb_table" "targets" {
  name           = "uptime-targets-dev"
  billing_mode   = "PROVISIONED"
  read_capacity  = 5
  write_capacity = 5
  hash_key       = "url"

  attribute {
    name = "url"
    type = "S"
  }
}