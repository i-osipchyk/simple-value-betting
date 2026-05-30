terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket               = "polymarket-terraform-state"
    key                  = "polymarket/terraform.tfstate"
    region               = "eu-central-1"
    dynamodb_table       = "polymarket-terraform-locks"
    encrypt              = true
    workspace_key_prefix = "workspaces"
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  name_prefix = "polymarket-${var.environment}"
  common_tags = {
    Environment = var.environment
    Project     = "polymarket-ml"
    ManagedBy   = "terraform"
  }
}
