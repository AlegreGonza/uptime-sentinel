variable "region" {
  type    = string
  default = "us-east-1"
}

variable "env" {
  type = string
}

variable "runtime" {
  type    = string
  default = "python3.13"
}

variable "lambda_timeout" {
  type    = number
  default = 15
}

variable "src_path" {
  type    = string
  default = "../../../src/checker.py"
}

variable "alert_email" {
  type = string
}