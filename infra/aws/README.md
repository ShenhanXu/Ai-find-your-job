# AWS Deployment Plan

This project is ready to deploy in a small, portfolio-friendly AWS setup:

1. Frontend: AWS Amplify Hosting or S3 + CloudFront.
2. API: App Runner or ECS Fargate running the FastAPI container.
3. Database: RDS PostgreSQL with the pgvector extension enabled.
4. Cache and jobs: ElastiCache Redis.
5. Secrets: AWS Secrets Manager for `OPENAI_API_KEY`, `DATABASE_URL`, and `REDIS_URL`.
6. Observability: CloudWatch logs, metrics, and API alarms.

For a first public demo, App Runner + RDS + ElastiCache is the cleanest path. For a lower-cost demo, run the API and web containers on a single small EC2 instance and keep RDS managed.

