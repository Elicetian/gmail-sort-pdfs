output "lambda_arn" {
  value = aws_lambda_function.mail_sort_pdfs.arn
}

output "lambda_name" {
  value = aws_lambda_function.mail_sort_pdfs.function_name
}
