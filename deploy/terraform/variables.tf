variable "environment" {
  description = "Environment name (e.g. production, staging)"
  type        = string
  default     = "production"
}

variable "hosted_zone_id" {
  description = "Route53 hosted zone ID for nexflo.ai"
  type        = string
}

variable "domain_name" {
  description = "Custom domain for the buyer service"
  type        = string
  default     = "buyer.nexflo.ai"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "desired_count" {
  description = "Number of ECS tasks to run"
  type        = number
  default     = 1
}

variable "task_cpu" {
  description = "ECS task CPU units (256 = 0.25 vCPU)"
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "ECS task memory in MB"
  type        = number
  default     = 1024
}

variable "aurora_min_capacity" {
  description = "Aurora Serverless v2 minimum ACUs"
  type        = number
  default     = 0.5
}

variable "aurora_max_capacity" {
  description = "Aurora Serverless v2 maximum ACUs"
  type        = number
  default     = 4
}

variable "db_name" {
  description = "PostgreSQL database name"
  type        = string
  default     = "nxflo"
}

variable "db_username" {
  description = "PostgreSQL master username"
  type        = string
  default     = "nxflo_admin"
}
