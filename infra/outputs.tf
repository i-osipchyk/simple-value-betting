output "collector_ecr_url" {
  description = "ECR repository URL for the collector image"
  value       = aws_ecr_repository.collector.repository_url
}

output "trade_engine_ecr_url" {
  description = "ECR repository URL for the trade_engine image"
  value       = aws_ecr_repository.trade_engine.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  description = "ECS service name"
  value       = aws_ecs_service.main.name
}

output "asg_name" {
  description = "Auto Scaling group name — use to find instance ID for SSM session"
  value       = aws_autoscaling_group.ecs.name
}

output "github_actions_role_arn" {
  description = "IAM role ARN — add as AWS_ROLE_ARN GitHub Actions secret"
  value       = aws_iam_role.github_actions.arn
}

output "s3_bucket_name" {
  description = "S3 bucket for parquet data"
  value       = aws_s3_bucket.data.bucket
}
