terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  default = "us-east-1"
}

variable "environment" {
  default = "prod"
}

resource "aws_s3_bucket" "analytics" {
  bucket = "lendwise-analytics-${var.environment}"

  tags = {
    Environment = var.environment
    Owner       = "data-platform"
  }
}

# 16 CFR 314.4(c)(3): encrypt customer information at rest.
resource "aws_s3_bucket_server_side_encryption_configuration" "analytics" {
  bucket = aws_s3_bucket.analytics.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

# 16 CFR 314.4(c)(6)(i): dispose of customer information no later than two
# years after last use.
resource "aws_s3_bucket_lifecycle_configuration" "analytics" {
  bucket = aws_s3_bucket.analytics.id

  rule {
    id     = "expire-exports-after-two-years"
    status = "Enabled"

    filter {}

    expiration {
      days = 730
    }
  }
}

resource "aws_s3_bucket_public_access_block" "analytics" {
  bucket = aws_s3_bucket.analytics.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
