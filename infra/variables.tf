variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "eu-central-1"
}

variable "environment" {
  description = "Deployment environment"
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be 'dev' or 'prod'"
  }
}

variable "ec2_instance_type" {
  description = "EC2 instance type for ECS container instances"
  type        = string
}

variable "s3_bucket_name" {
  description = "S3 bucket name for parquet file storage"
  type        = string
}

variable "github_repo" {
  description = "GitHub repository in owner/name format, e.g. 'ivanosipchyk/simple-value-betting'"
  type        = string
}
