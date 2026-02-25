# Nexflo AWS Infrastructure Snapshot

**Date:** 2026-02-25
**Region:** us-east-1
**Account:** 040767100685
**Purpose:** Full config capture before parking all infrastructure. Use this to spin everything back up.

---

## 1. VPC & Networking

### VPC
| Key | Value |
|-----|-------|
| VPC ID | `vpc-01bc0a0237a099b01` |
| CIDR | `10.1.0.0/16` |
| Name | `nxflo-buyer-production` |

### Subnets
| Name | Subnet ID | CIDR | AZ |
|------|-----------|------|----|
| nxflo-buyer-public-a | `subnet-0d26b359cdf99ba90` | 10.1.1.0/24 | us-east-1a |
| nxflo-buyer-public-b | `subnet-0ca2ddc5545db760f` | 10.1.2.0/24 | us-east-1b |
| nxflo-buyer-private-a | `subnet-02f99724825b0240d` | 10.1.10.0/24 | us-east-1a |
| nxflo-buyer-private-b | `subnet-09324f56e61ecb33c` | 10.1.11.0/24 | us-east-1b |

### NAT Gateway (TO BE DELETED)
| Key | Value |
|-----|-------|
| NAT Gateway ID | `nat-096df59d3d4655cd0` |
| Subnet | `subnet-0d26b359cdf99ba90` (public-a) |
| Public IP | `3.90.171.144` |
| Elastic IP | `eipalloc-024090b37f26380f6` |

**To recreate:** Create NAT Gateway in public-a subnet, allocate new EIP, update private subnet route tables.

---

## 2. Aurora PostgreSQL (TO BE STOPPED)

### Cluster
| Key | Value |
|-----|-------|
| Cluster ID | `nxflo-db` |
| Engine | Aurora PostgreSQL 16.4 |
| Endpoint | `nxflo-db.cluster-c2vymsmgm54m.us-east-1.rds.amazonaws.com` |
| Reader Endpoint | `nxflo-db.cluster-ro-c2vymsmgm54m.us-east-1.rds.amazonaws.com` |
| Port | 5432 |
| Database Name | `nxflo` |
| Master User | `nxflo_admin` |
| Serverless v2 | Min 0.5 ACU, Max 4.0 ACU |
| Subnet Group | `nxflo-db-subnets` |
| Security Group | `sg-07508c98ce4c89d03` (nxflo-db-sg) |
| KMS Key | `cfd51f2d-5f06-4b88-a3f4-a058d603c175` |
| Deletion Protection | ON |
| Backup Retention | 7 days |
| Backup Window | 03:00-04:00 UTC |
| CloudWatch Logs | postgresql |

### Writer Instance
| Key | Value |
|-----|-------|
| Instance ID | `nxflo-db-writer` |
| Class | db.serverless |
| AZ | us-east-1a |
| Performance Insights | ON (7 day retention) |

### RDS Proxies
| Name | Endpoint |
|------|----------|
| nxflo-db-proxy | `nxflo-db-proxy.proxy-c2vymsmgm54m.us-east-1.rds.amazonaws.com` |
| nxflo-buyer-proxy | `nxflo-buyer-proxy.proxy-c2vymsmgm54m.us-east-1.rds.amazonaws.com` |

**DB password:** Stored in `nxflo/buyer/aurora-password` in Secrets Manager. Value: `[REDACTED]` (auto-rotation OFF).

**Snapshot name:** `nxflo-db-parking-20260225`

---

## 3. ECS Services

All services are Fargate, in private subnets, behind ALBs.

### nxflo-buyer
| Key | Value |
|-----|-------|
| Cluster | `nxflo-buyer-cluster` |
| Service | `nxflo-buyer-service` |
| Task Def | `nxflo-buyer:2` |
| CPU/Memory | 512 / 1024 |
| Image | `040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-buyer:latest` |
| Port | 8000 |
| Security Group | `sg-0e0547642264ce68b` |
| Subnets | private-a, private-b |
| Log Group | `/ecs/nxflo-buyer` |
| Env Vars | NXFLO_PORT=8000, NXFLO_HOST=0.0.0.0, NXFLO_WEBHOOK_BASE_URL=https://buyer.nexflo.ai |
| Secrets | NXFLO_DATABASE_URL → `nxflo/buyer/database-url`, NXFLO_WEBHOOK_SECRET → `nxflo/buyer/webhook-secret` |
| ALB | `nxflo-buyer-alb` → Target Group `nxflo-buyer-tg/6a4e1650fba0b996` |

### nxflo-dsp
| Key | Value |
|-----|-------|
| Cluster | `nxflo-dsp-cluster` |
| Service | `nxflo-dsp-service` |
| Task Def | `nxflo-dsp:3` |
| CPU/Memory | 1024 / 2048 |
| Image | `040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-dsp:rename-20260214` |
| Port | 8080 |
| Security Group | `sg-0a10c1f63a4ee4681` |
| Subnets | private-a, private-b |
| Log Group | `/ecs/nxflo-dsp` |
| Env Vars | NXFLO_CONFIG_TABLE=nxflo-dsp-config, RUST_LOG=info, NXFLO_FIREHOSE_STREAM=nxflo-bid-events, MALLOC_ARENA_MAX=2 |
| Secrets | NXFLO_WIN_SECRET → `nxflo/dsp/win-secret`, DATABASE_URL → `nxflo/dsp/database-url` |
| ALB | `nxflo-dsp-alb` → Target Group `nxflo-dsp-tg/f029fcf5115463dd` |

### nxflo-pbs
| Key | Value |
|-----|-------|
| Cluster | `nxflo-pbs-cluster` |
| Service | `nxflo-pbs-service` |
| Task Def | `nxflo-pbs:1` |
| CPU/Memory | 1024 / 2048 |
| Image | `040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-pbs:latest` |
| Port | 8000 |
| Security Group | `sg-0b2c63ac13dc63a73` |
| Subnets | private-a, private-b |
| Log Group | `/ecs/nxflo-pbs` |
| Env Vars | (none) |
| Secrets | (none) |
| ALB | `nxflo-pbs-alb` → Target Group `nxflo-pbs-tg/da55196aa22d4a63` |

### nxflo-axe
| Key | Value |
|-----|-------|
| Cluster | `nxflo-axe-cluster` |
| Service | `nxflo-axe-service` |
| Task Def | `nxflo-axe:1` |
| CPU/Memory | 512 / 1024 |
| Image | `040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-axe:latest` |
| Port | 8000 |
| Security Group | `sg-087b68c2c4c553807` |
| Subnets | private-a, private-b |
| Log Group | `/ecs/nxflo-axe` |
| Env Vars | NXFLO_AXE_HOST=0.0.0.0, NXFLO_AXE_PORT=8000 |
| Secrets | NXFLO_AXE_DATABASE_URL → `nxflo/axe/database-url` |
| ALB | `nxflo-axe-alb` → Target Group `nxflo-axe-tg/d3d4c7a612410e56` |

### nxflo-creative
| Key | Value |
|-----|-------|
| Cluster | `nxflo-creative-cluster` |
| Service | `nxflo-creative-service` |
| Task Def | `nxflo-creative:1` |
| CPU/Memory | 512 / 1024 |
| Image | `040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-creative:latest` |
| Port | 8000 |
| Security Group | `sg-07ff35bee91e07eb3` |
| Subnets | private-a, private-b |
| Log Group | `/ecs/nxflo-creative` |
| Env Vars | NXFLO_CREATIVE_HOST=0.0.0.0, NXFLO_CREATIVE_PORT=8000 |
| Secrets | NXFLO_CREATIVE_DATABASE_URL → `nxflo/creative/database-url` |
| ALB | `nxflo-creative-alb` → Target Group `nxflo-creative-tg/80efbfc8e712b311` |

### nxflo-signals
| Key | Value |
|-----|-------|
| Cluster | `nxflo-signals-cluster` |
| Service | `nxflo-signals-service` |
| Task Def | `nxflo-signals:1` |
| CPU/Memory | 512 / 1024 |
| Image | `040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-signals:latest` |
| Port | 8000 |
| Security Group | `sg-094ac92d064a25ad7` |
| Subnets | private-a, private-b |
| Log Group | `/ecs/nxflo-signals` |
| Env Vars | NXFLO_SIGNALS_HOST=0.0.0.0, NXFLO_SIGNALS_PORT=8000 |
| Secrets | NXFLO_SIGNALS_DATABASE_URL → `nxflo/signals/database-url` |
| ALB | `nxflo-signals-alb` → Target Group `nxflo-signals-tg/872bf51b2c8f9af1` |

### nxflo-report
| Key | Value |
|-----|-------|
| Cluster | `nxflo-report-cluster` |
| Service | `nxflo-report-service` |
| Task Def | `nxflo-report:1` |
| CPU/Memory | 512 / 1024 |
| Image | `040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-report:latest` |
| Port | 8000 |
| Security Group | `sg-0cf2cec857cf80aa8` |
| Subnets | private-a, private-b |
| Log Group | `/ecs/nxflo-report` |
| Env Vars | NXFLO_REPORT_HOST=0.0.0.0, NXFLO_REPORT_PORT=8000 |
| Secrets | NXFLO_REPORT_DATABASE_URL → `nxflo/report/database-url` |
| ALB | `nxflo-report-alb` → Target Group `nxflo-report-tg/b92ed25d2dad833f` |

---

## 4. Load Balancers (TO BE DELETED)

| ALB Name | DNS | VPC | Security Group | Subnets |
|----------|-----|-----|----------------|---------|
| nxflo-buyer-alb | nxflo-buyer-alb-103918942.us-east-1.elb.amazonaws.com | vpc-01bc0a0237a099b01 | sg-047fbbdec2f6cfe83 | public-a, public-b |
| nxflo-dsp-alb | nxflo-dsp-alb-1207635221.us-east-1.elb.amazonaws.com | vpc-01bc0a0237a099b01 | sg-09c8e3bb7974146ab | public-a, public-b |
| nxflo-pbs-alb | nxflo-pbs-alb-2014741004.us-east-1.elb.amazonaws.com | vpc-01bc0a0237a099b01 | sg-03b28bb7efb96a7ce | public-a, public-b |
| nxflo-axe-alb | nxflo-axe-alb-2042397193.us-east-1.elb.amazonaws.com | vpc-01bc0a0237a099b01 | sg-082d2a5d55eafe3f4 | public-a, public-b |
| nxflo-creative-alb | nxflo-creative-alb-1368960111.us-east-1.elb.amazonaws.com | vpc-01bc0a0237a099b01 | sg-079f0dbcaca26d360 | public-a, public-b |
| nxflo-signals-alb | nxflo-signals-alb-1374592892.us-east-1.elb.amazonaws.com | vpc-01bc0a0237a099b01 | sg-047a7d9ccbbbfc9bc | public-a, public-b |
| nxflo-report-alb | nxflo-report-alb-1733322694.us-east-1.elb.amazonaws.com | vpc-01bc0a0237a099b01 | sg-02f829ea1bce481b6 | public-a, public-b |
| axp-prizm-alb | axp-prizm-alb-1159712172.us-east-1.elb.amazonaws.com | vpc-0e4565e7552f9ac71 | sg-022499d3d652056b9 | (legacy VPC) |

**Note:** Each ALB has HTTPS (443) listener with ACM cert + HTTP (80) redirect. Target groups route to ECS tasks on their respective ports.

---

## 5. ECR Repositories (KEEP)

| Repo | URI |
|------|-----|
| nxflo-buyer | 040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-buyer |
| nxflo-dsp | 040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-dsp |
| nxflo-pbs | 040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-pbs |
| nxflo-axe | 040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-axe |
| nxflo-creative | 040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-creative |
| nxflo-signals | 040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-signals |
| nxflo-report | 040767100685.dkr.ecr.us-east-1.amazonaws.com/nxflo-report |
| axp-prizm-core | 040767100685.dkr.ecr.us-east-1.amazonaws.com/axp-prizm-core (legacy) |

---

## 6. Secrets Manager (KEEP)

| Secret | ARN |
|--------|-----|
| nxflo/buyer/database-url | arn:aws:secretsmanager:us-east-1:040767100685:secret:nxflo/buyer/database-url-zcq0sz |
| nxflo/buyer/aurora-password | arn:aws:secretsmanager:us-east-1:040767100685:secret:nxflo/buyer/aurora-password-uxJhDL |
| nxflo/buyer/webhook-secret | arn:aws:secretsmanager:us-east-1:040767100685:secret:nxflo/buyer/webhook-secret-6WUemU |
| nxflo/dsp/database-url | arn:aws:secretsmanager:us-east-1:040767100685:secret:nxflo/dsp/database-url-7I6uqD |
| nxflo/dsp/win-secret | arn:aws:secretsmanager:us-east-1:040767100685:secret:nxflo/dsp/win-secret-46Gijm |
| nxflo/axe/database-url | arn:aws:secretsmanager:us-east-1:040767100685:secret:nxflo/axe/database-url-pu5I3V |
| nxflo/signals/database-url | arn:aws:secretsmanager:us-east-1:040767100685:secret:nxflo/signals/database-url-tZJ0z6 |
| nxflo/creative/database-url | arn:aws:secretsmanager:us-east-1:040767100685:secret:nxflo/creative/database-url-zJ1ruJ |
| nxflo/report/database-url | arn:aws:secretsmanager:us-east-1:040767100685:secret:nxflo/report/database-url-N2fO24 |

---

## 7. ACM Certificates (KEEP - free)

| Domain | ARN |
|--------|-----|
| buyer.nexflo.ai | arn:aws:acm:us-east-1:040767100685:certificate/15d4b9cb-c86f-43b6-b35a-60c65902f548 |
| dsp.nexflo.ai | arn:aws:acm:us-east-1:040767100685:certificate/614be508-df08-47be-aab2-6ef660f7644a |
| pbs.nexflo.ai | arn:aws:acm:us-east-1:040767100685:certificate/c392630d-d2bd-4786-b361-0ad676b482d1 |
| axe.nexflo.ai | arn:aws:acm:us-east-1:040767100685:certificate/3db4ef30-fec4-48ca-944f-f5256db19b04 |
| creative.nexflo.ai | arn:aws:acm:us-east-1:040767100685:certificate/519e6cde-c1c2-4be9-abc7-aaabebc03ada |
| signals.nexflo.ai | arn:aws:acm:us-east-1:040767100685:certificate/5738ddcb-c2ef-4aa6-b3e1-d11ebf7368e0 |
| report.nexflo.ai | arn:aws:acm:us-east-1:040767100685:certificate/f48c62fe-9708-4295-a1a9-693f1a28b8e3 |
| static.nexflo.ai | arn:aws:acm:us-east-1:040767100685:certificate/c4d74d01-d052-4ef1-95df-4383b9168ef0 |
| cdn.nexflo.ai | arn:aws:acm:us-east-1:040767100685:certificate/7d882ffb-b650-47a9-b984-9b0c6399faad |
| dsp.adfx.io | arn:aws:acm:us-east-1:040767100685:certificate/c69f086f-eced-4dd8-a45b-ebdd1728442b |
| adfx.io | arn:aws:acm:us-east-1:040767100685:certificate/cd34c37c-ba45-449f-b9d5-d6af9828dd1d |

---

## 8. CloudFront (KEEP - serves publisher assets)

| Distribution ID | Domain | Alias |
|----------------|--------|-------|
| E263RAV33CI0Y3 | d9hi8104bku2g.cloudfront.net | static.nexflo.ai |

**Origin:** S3 bucket `nxflo-assets`

---

## 9. S3 Buckets (KEEP)

| Bucket | Purpose |
|--------|---------|
| nxflo-assets | Static assets (prebid-wrapper.js, creatives) served via CloudFront |
| nxflo-bid-events-040767100685 | Firehose destination for bid event logs |
| nxflo-deploy-040767100685 | Deploy artifacts |

---

## 10. DynamoDB Tables (KEEP - pennies)

| Table | Purpose |
|-------|---------|
| nxflo-dsp-config | DSP campaign/mission configuration |
| axp-prizm-config | Legacy DSP config |
| axp-budget-reservoir | Legacy budget tracking |

---

## 11. Firehose

| Stream | Destination |
|--------|-------------|
| nxflo-bid-events | S3 bucket `nxflo-bid-events-040767100685` |

---

## 12. IAM Roles (KEEP - free)

| Role | Purpose |
|------|---------|
| nxflo-buyer-ecs-execution-production | ECS exec role (pulls ECR, reads secrets) |
| nxflo-buyer-ecs-task-production | ECS task role (app permissions) |
| nxflo-buyer-github-actions | CI/CD OIDC (trust: repo:alaplante01/nxflo-buyer:*) |
| nxflo-buyer-rds-proxy-production | RDS proxy auth |
| nxflo-dsp-exec-role / nxflo-dsp-task-role | DSP ECS roles |
| nxflo-pbs-exec-role / nxflo-pbs-task-role | PBS ECS roles |
| nxflo-axe-exec-role-prod / nxflo-axe-task-role-prod | AXE ECS roles |
| nxflo-creative-exec-role-prod / nxflo-creative-task-role-prod | Creative ECS roles |
| nxflo-signals-exec-role-prod / nxflo-signals-task-role-prod | Signals ECS roles |
| nxflo-report-exec-role-prod / nxflo-report-task-role-prod | Report ECS roles |
| nxflo-db-proxy-role | RDS proxy role |
| nxflo-firehose-role | Firehose delivery role |

---

## 13. Security Groups

| SG ID | Name | Purpose |
|-------|------|---------|
| sg-07508c98ce4c89d03 | nxflo-db-sg | Aurora - accept from ECS tasks only |
| sg-0e0547642264ce68b | nxflo-buyer-task | ECS Task: inbound from ALB |
| sg-047fbbdec2f6cfe83 | nxflo-buyer-alb | ALB: inbound HTTP/HTTPS |
| sg-0a10c1f63a4ee4681 | nxflo-dsp-task-sg | ECS tasks - ALB traffic only |
| sg-09c8e3bb7974146ab | nxflo-dsp-alb-sg | ALB ingress (HTTPS/HTTP redirect) |
| sg-0b2c63ac13dc63a73 | nxflo-pbs-task-sg | PBS ECS task |
| sg-03b28bb7efb96a7ce | nxflo-pbs-alb-sg | PBS ALB |
| sg-087b68c2c4c553807 | nxflo-axe-task | AXE ECS task |
| sg-082d2a5d55eafe3f4 | nxflo-axe-alb | AXE ALB |
| sg-07ff35bee91e07eb3 | nxflo-creative-task | Creative ECS task |
| sg-079f0dbcaca26d360 | nxflo-creative-alb | Creative ALB |
| sg-094ac92d064a25ad7 | nxflo-signals-task | Signals ECS task |
| sg-047a7d9ccbbbfc9bc | nxflo-signals-alb | Signals ALB |
| sg-0cf2cec857cf80aa8 | nxflo-report-task | Report ECS task |
| sg-02f829ea1bce481b6 | nxflo-report-alb | Report ALB |
| sg-0b4fa9e51d9c1c97c | nxflo-buyer-vpce | VPC Endpoints |
| sg-0531907d0295d6cdf | nxflo-buyer-aurora | Aurora from ECS |

---

## 14. DNS (Route53)

**Hosted Zone:** nexflo.ai (AWS nameservers)

### Active Records
| Record | Type | Target |
|--------|------|--------|
| nexflo.ai | MX | Google Workspace (1 ASPMX.L.GOOGLE.COM, etc.) |
| nexflo.ai | TXT | v=spf1 include:_spf.google.com ~all |
| buyer.nexflo.ai | A (alias) | nxflo-buyer-alb |
| dsp.nexflo.ai | A (alias) | nxflo-dsp-alb |
| pbs.nexflo.ai | A (alias) | nxflo-pbs-alb |
| axe.nexflo.ai | A (alias) | nxflo-axe-alb |
| creative.nexflo.ai | A (alias) | nxflo-creative-alb |
| signals.nexflo.ai | A (alias) | nxflo-signals-alb |
| report.nexflo.ai | A (alias) | nxflo-report-alb |
| static.nexflo.ai | A (alias) | CloudFront d9hi8104bku2g.cloudfront.net |

### ACM Validation CNAMEs (keep for cert renewal)
Multiple _acm-validations records — do not delete.

---

## 15. What to Delete (saves ~$330/mo)

| Resource | Monthly Cost | Action |
|----------|-------------|--------|
| NAT Gateway nat-096df59d3d4655cd0 | ~$45 | Delete + release EIP |
| 7x nxflo ALBs + axp-prizm-alb | ~$130 | Delete ALBs + target groups |
| Aurora cluster nxflo-db | ~$120 | Snapshot + stop |
| RDS Proxies (2x) | ~$30 | Delete |
| AWS Business Support | ~$54 | Downgrade plan |

## 16. What to Keep (free or pennies)

| Resource | Reason |
|----------|--------|
| VPC + subnets + SGs + route tables | Free, needed for spin-up |
| ECR repos with images | Pennies for storage |
| ACM certificates | Free |
| IAM roles | Free |
| Secrets Manager (9 secrets) | ~$3.60/mo total |
| S3 buckets | Pennies |
| DynamoDB tables | Pennies |
| CloudFront (static.nexflo.ai) | Keep for publisher assets |
| Route53 hosted zone | $0.50/mo |
| DNS A records | Will break (ALBs deleted) — remove them |

---

## Spin-Up Checklist

When ready to bring everything back online:

1. **Create NAT Gateway** in public-a subnet, allocate EIP, update private route tables
2. **Start Aurora** — `aws rds start-db-cluster --db-cluster-identifier nxflo-db`
3. **Recreate ALBs** — one per service, HTTPS listener with ACM cert, HTTP redirect, target group on service port
4. **Update DNS** — point subdomain A records at new ALBs
5. **Scale ECS services** — `aws ecs update-service --cluster X --service Y --desired-count 1`
6. **Recreate RDS Proxies** if needed
7. **Verify health endpoints** for each service
