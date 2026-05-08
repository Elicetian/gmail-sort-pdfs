terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
  }

  backend "s3" {
    key    = "mail-sort-pdfs/terraform.tfstate"
    region = "eu-west-1"
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = {
      PRODUCT = "mail-sort-pdfs"
    }
  }
}

# ── Build ────────────────────────────────────────────────────────────────────

resource "null_resource" "pip_install" {
  triggers = {
    requirements = filemd5("${path.module}/lambda/requirements.txt")
    handler      = filemd5("${path.module}/lambda/handler.py")
  }

  provisioner "local-exec" {
    command = <<-EOT
      mkdir -p ${path.module}/lambda_build
      uv pip install -r ${path.module}/lambda/requirements.txt \
        --target ${path.module}/lambda_build/ \
        --python-version 3.12 \
        --python-platform x86_64-unknown-linux-gnu \
        --quiet
      cp ${path.module}/lambda/handler.py ${path.module}/lambda_build/
    EOT
  }
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda_build"
  output_path = "${path.module}/lambda.zip"
  depends_on  = [null_resource.pip_install]
}

# ── Lambda ───────────────────────────────────────────────────────────────────

resource "aws_iam_role" "lambda" {
  name = "mail-sort-pdfs-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "mail-sort-pdfs-lambda-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:eu-west-1:*:log-group:/aws/lambda/mail-sort-pdfs:*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:eu-west-1:*:parameter/mail-sort-pdfs/*"
      },
    ]
  })
}

resource "aws_lambda_function" "mail_sort_pdfs" {
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  function_name    = "mail-sort-pdfs"
  role             = aws_iam_role.lambda.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 120
  memory_size      = 256

}

# ── Scheduler ────────────────────────────────────────────────────────────────

resource "aws_iam_role" "scheduler" {
  name = "mail-sort-pdfs-scheduler-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "mail-sort-pdfs-scheduler-policy"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.mail_sort_pdfs.arn
    }]
  })
}

resource "aws_scheduler_schedule" "mail_sort_pdfs" {
  name                         = "mail-sort-pdfs-daily"
  schedule_expression          = "cron(0 8 * * ? *)"
  schedule_expression_timezone = "Europe/Paris"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.mail_sort_pdfs.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

# ── Alarme ───────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "mail-sort-pdfs-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 86400
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.mail_sort_pdfs.function_name
  }
}
