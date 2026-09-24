output "state_bucket" {
  value       = aws_s3_bucket.state.id
  description = "Temporary remote-state bucket for the lab root."
}

output "release_bucket" {
  value       = aws_s3_bucket.releases.id
  description = "Private bucket for immutable target release artifacts."
}

output "account_id" {
  value = data.aws_caller_identity.current.account_id
}
