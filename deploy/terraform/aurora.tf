# --- Aurora Serverless v2 (PostgreSQL) ---

resource "aws_rds_cluster" "buyer" {
  cluster_identifier = "nxflo-buyer-db"
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned"
  engine_version     = "16.4"
  database_name      = var.db_name
  master_username    = var.db_username
  master_password    = random_password.aurora.result

  vpc_security_group_ids = [aws_security_group.aurora.id]
  db_subnet_group_name   = aws_db_subnet_group.buyer.name

  serverlessv2_scaling_configuration {
    min_capacity = var.aurora_min_capacity
    max_capacity = var.aurora_max_capacity
  }

  storage_encrypted   = true
  deletion_protection = true
  skip_final_snapshot = false
  final_snapshot_identifier = "nxflo-buyer-db-final"

  backup_retention_period = 7
  preferred_backup_window = "03:00-04:00"

  tags = {
    Name = "nxflo-buyer-db"
  }
}

resource "aws_rds_cluster_instance" "writer" {
  identifier         = "nxflo-buyer-db-writer"
  cluster_identifier = aws_rds_cluster.buyer.id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.buyer.engine
  engine_version     = aws_rds_cluster.buyer.engine_version

  tags = {
    Name = "nxflo-buyer-db-writer"
  }
}

resource "aws_db_subnet_group" "buyer" {
  name       = "nxflo-buyer-db"
  subnet_ids = [aws_subnet.private_a.id, aws_subnet.private_b.id]

  tags = {
    Name = "nxflo-buyer-db"
  }
}

# --- RDS Proxy ---

resource "aws_db_proxy" "buyer" {
  name                   = "nxflo-buyer-proxy"
  debug_logging          = false
  engine_family          = "POSTGRESQL"
  idle_client_timeout    = 1800
  require_tls            = true
  role_arn               = aws_iam_role.rds_proxy.arn
  vpc_security_group_ids = [aws_security_group.aurora.id]
  vpc_subnet_ids         = [aws_subnet.private_a.id, aws_subnet.private_b.id]

  auth {
    auth_scheme = "SECRETS"
    iam_auth    = "DISABLED"
    secret_arn  = aws_secretsmanager_secret.aurora_password.arn
  }

  tags = {
    Name = "nxflo-buyer-proxy"
  }
}

resource "aws_db_proxy_default_target_group" "buyer" {
  db_proxy_name = aws_db_proxy.buyer.name

  connection_pool_config {
    max_connections_percent      = 100
    max_idle_connections_percent = 50
    connection_borrow_timeout    = 120
  }
}

resource "aws_db_proxy_target" "buyer" {
  db_proxy_name         = aws_db_proxy.buyer.name
  target_group_name     = aws_db_proxy_default_target_group.buyer.name
  db_cluster_identifier = aws_rds_cluster.buyer.id
}
