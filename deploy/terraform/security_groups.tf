# ALB — accepts HTTPS/HTTP from the internet
resource "aws_security_group" "alb" {
  name_prefix = "nxflo-buyer-alb-"
  vpc_id      = aws_vpc.main.id
  description = "ALB: inbound HTTP/HTTPS from internet"

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTPS"
  }

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTP (redirects to HTTPS)"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "All outbound"
  }

  tags = {
    Name = "nxflo-buyer-alb-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# ECS Task — accepts traffic from ALB only, outbound to internet (seller MCP calls)
resource "aws_security_group" "task" {
  name_prefix = "nxflo-buyer-task-"
  vpc_id      = aws_vpc.main.id
  description = "ECS Task: inbound from ALB, outbound to internet"

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
    description     = "From ALB"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "All outbound (seller MCP calls, AWS APIs)"
  }

  tags = {
    Name = "nxflo-buyer-task-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Aurora — accepts connections from ECS tasks and RDS Proxy only
resource "aws_security_group" "aurora" {
  name_prefix = "nxflo-buyer-aurora-"
  vpc_id      = aws_vpc.main.id
  description = "Aurora: inbound from ECS tasks and RDS Proxy"

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
    description     = "From ECS tasks"
  }

  ingress {
    from_port = 5432
    to_port   = 5432
    protocol  = "tcp"
    self      = true
    description = "RDS Proxy to Aurora (self-referencing)"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "All outbound"
  }

  tags = {
    Name = "nxflo-buyer-aurora-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# VPC Endpoints — accepts HTTPS from ECS tasks
resource "aws_security_group" "vpc_endpoints" {
  name_prefix = "nxflo-buyer-vpce-"
  vpc_id      = aws_vpc.main.id
  description = "VPC Endpoints: inbound HTTPS from ECS tasks"

  ingress {
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
    description     = "From ECS tasks"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "All outbound"
  }

  tags = {
    Name = "nxflo-buyer-vpce-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}
