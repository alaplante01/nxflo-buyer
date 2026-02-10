terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "nxflo-buyer"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# --- ECR Repository ---

resource "aws_ecr_repository" "buyer" {
  name                 = "nxflo-buyer"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "buyer" {
  repository = aws_ecr_repository.buyer.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = {
        type = "expire"
      }
    }]
  })
}

# --- Secrets Manager ---

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "nxflo/buyer/database-url"
  description             = "PostgreSQL connection string for Nexflo Buyer"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "webhook_secret" {
  name                    = "nxflo/buyer/webhook-secret"
  description             = "HMAC secret for webhook authentication"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "aurora_password" {
  name                    = "nxflo/buyer/aurora-password"
  description             = "Aurora master password"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "aurora_password" {
  secret_id = aws_secretsmanager_secret.aurora_password.id
  secret_string = jsonencode({
    username = var.db_username
    password = random_password.aurora.result
  })
}

resource "random_password" "aurora" {
  length  = 32
  special = false
}

# --- CloudWatch ---

resource "aws_cloudwatch_log_group" "buyer" {
  name              = "/ecs/nxflo-buyer"
  retention_in_days = 30
}

# --- ALB ---

resource "aws_lb" "buyer" {
  name               = "nxflo-buyer-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = [aws_subnet.public_a.id, aws_subnet.public_b.id]

  tags = {
    Name = "nxflo-buyer-alb"
  }
}

resource "aws_lb_target_group" "buyer" {
  name        = "nxflo-buyer-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/health"
    port                = "traffic-port"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  deregistration_delay = 30
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.buyer.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.buyer.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.buyer.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.buyer.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# --- ECS Cluster ---

resource "aws_ecs_cluster" "buyer" {
  name = "nxflo-buyer-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# --- ECS Task Definition ---

resource "aws_ecs_task_definition" "buyer" {
  family                   = "nxflo-buyer"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = "buyer"
    image     = "${aws_ecr_repository.buyer.repository_url}:latest"
    essential = true

    portMappings = [{
      containerPort = 8000
      protocol      = "tcp"
    }]

    environment = [
      { name = "NXFLO_HOST", value = "0.0.0.0" },
      { name = "NXFLO_PORT", value = "8000" },
      { name = "NXFLO_WEBHOOK_BASE_URL", value = "https://${var.domain_name}" },
    ]

    secrets = [
      {
        name      = "NXFLO_DATABASE_URL"
        valueFrom = aws_secretsmanager_secret.database_url.arn
      },
      {
        name      = "NXFLO_WEBHOOK_SECRET"
        valueFrom = aws_secretsmanager_secret.webhook_secret.arn
      },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.buyer.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "buyer"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\" || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
  }])
}

# --- ECS Service ---

resource "aws_ecs_service" "buyer" {
  name            = "nxflo-buyer-service"
  cluster         = aws_ecs_cluster.buyer.id
  task_definition = aws_ecs_task_definition.buyer.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [aws_subnet.private_a.id, aws_subnet.private_b.id]
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.buyer.arn
    container_name   = "buyer"
    container_port   = 8000
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  depends_on = [aws_lb_listener.https]
}
