output "ecr_repository_url" {
  description = "ECR repository URL for Docker images"
  value       = aws_ecr_repository.buyer.repository_url
}

output "alb_dns_name" {
  description = "ALB DNS name"
  value       = aws_lb.buyer.dns_name
}

output "service_url" {
  description = "Public URL of the buyer service"
  value       = "https://${var.domain_name}"
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.buyer.name
}

output "ecs_service_name" {
  description = "ECS service name"
  value       = aws_ecs_service.buyer.name
}

output "rds_proxy_endpoint" {
  description = "RDS Proxy endpoint for database connections"
  value       = aws_db_proxy.buyer.endpoint
}

output "deploy_commands" {
  description = "Commands to deploy a new version"
  value       = <<-EOT
    # Build and push
    docker build -t ${aws_ecr_repository.buyer.repository_url}:latest .
    docker push ${aws_ecr_repository.buyer.repository_url}:latest

    # Force new deployment
    aws ecs update-service \
      --cluster ${aws_ecs_cluster.buyer.name} \
      --service ${aws_ecs_service.buyer.name} \
      --force-new-deployment
  EOT
}
