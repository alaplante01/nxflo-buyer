data "aws_caller_identity" "current" {}

# --- ECS Execution Role (pulls images, reads secrets, writes logs) ---

resource "aws_iam_role" "ecs_execution" {
  name = "nxflo-buyer-ecs-execution-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })

  tags = {
    Name = "nxflo-buyer-ecs-execution"
  }
}

resource "aws_iam_role_policy_attachment" "ecs_execution_base" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "secrets-access"
  role = aws_iam_role.ecs_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "secretsmanager:GetSecretValue",
      ]
      Resource = [
        aws_secretsmanager_secret.database_url.arn,
        aws_secretsmanager_secret.webhook_secret.arn,
      ]
    }]
  })
}

# --- ECS Task Role (app-level permissions) ---

resource "aws_iam_role" "ecs_task" {
  name = "nxflo-buyer-ecs-task-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })

  tags = {
    Name = "nxflo-buyer-ecs-task"
  }
}

resource "aws_iam_role_policy" "ecs_task_cloudwatch" {
  name = "cloudwatch-metrics"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "cloudwatch:PutMetricData",
      ]
      Resource = "*"
      Condition = {
        StringEquals = {
          "cloudwatch:namespace" = "NxfloBuyer"
        }
      }
    }]
  })
}

# --- RDS Proxy Role ---

resource "aws_iam_role" "rds_proxy" {
  name = "nxflo-buyer-rds-proxy-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "rds.amazonaws.com"
      }
    }]
  })

  tags = {
    Name = "nxflo-buyer-rds-proxy"
  }
}

resource "aws_iam_role_policy" "rds_proxy_secrets" {
  name = "secrets-access"
  role = aws_iam_role.rds_proxy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "secretsmanager:GetSecretValue",
        "secretsmanager:GetResourcePolicy",
        "secretsmanager:DescribeSecret",
        "secretsmanager:ListSecretVersionIds",
      ]
      Resource = [
        aws_secretsmanager_secret.aurora_password.arn,
      ]
    }]
  })
}
