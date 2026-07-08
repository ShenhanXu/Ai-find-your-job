#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-$(aws configure get region || true)}"
AWS_REGION="${AWS_REGION:-us-west-2}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd aws
require_cmd jq
require_cmd curl

FRONTEND_ORIGIN="${1:-${FRONTEND_ORIGIN:-}}"
if [ -z "$FRONTEND_ORIGIN" ] && [ -f .deploy/vercel-url ]; then
  FRONTEND_ORIGIN="$(cat .deploy/vercel-url)"
fi

if [ -z "$FRONTEND_ORIGIN" ]; then
  echo "Missing Vercel URL. Pass it explicitly:" >&2
  echo "./infra/aws/update-ecs-frontend-origin.sh https://your-project.vercel.app" >&2
  exit 1
fi

if [[ "$FRONTEND_ORIGIN" =~ ^(https?://[^/]+) ]]; then
  FRONTEND_ORIGIN="${BASH_REMATCH[1]}"
fi
FRONTEND_ORIGIN="${FRONTEND_ORIGIN%/}"

FRONTEND_ORIGINS="${FRONTEND_ORIGINS:-http://localhost:3000,http://127.0.0.1:3000,${FRONTEND_ORIGIN}}"

DB_SECRET_ARN="$(aws secretsmanager describe-secret \
  --secret-id 'ai-find-your-job/database-url' \
  --region "$AWS_REGION" \
  --query 'ARN' \
  --output text)"
JINA_SECRET_ARN="$(aws secretsmanager describe-secret \
  --secret-id 'ai-find-your-job/jina-api-key' \
  --region "$AWS_REGION" \
  --query 'ARN' \
  --output text)"
DEEPSEEK_SECRET_ARN="$(aws secretsmanager describe-secret \
  --secret-id 'ai-find-your-job/deepseek-api-key' \
  --region "$AWS_REGION" \
  --query 'ARN' \
  --output text)"

POLICY_DOC="$(jq -n \
  --arg db "$DB_SECRET_ARN" \
  --arg jina "$JINA_SECRET_ARN" \
  --arg deepseek "$DEEPSEEK_SECRET_ARN" \
  '{
    Version: "2012-10-17",
    Statement: [{
      Effect: "Allow",
      Action: ["secretsmanager:GetSecretValue"],
      Resource: [$db, $jina, $deepseek]
    }]
  }')"

aws iam put-role-policy \
  --role-name ecsTaskExecutionRole \
  --policy-name ai-find-your-job-read-api-secrets \
  --policy-document "$POLICY_DOC"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/ai-find-your-job-api:latest"
EXEC_ROLE_ARN="$(aws iam get-role --role-name ecsTaskExecutionRole --query 'Role.Arn' --output text)"

CONTAINER_DEFINITIONS="$(jq -n \
  --arg image "$IMAGE_URI" \
  --arg region "$AWS_REGION" \
  --arg frontendOrigins "$FRONTEND_ORIGINS" \
  --arg db "$DB_SECRET_ARN" \
  --arg jina "$JINA_SECRET_ARN" \
  --arg deepseek "$DEEPSEEK_SECRET_ARN" \
  '[{
    name: "ai-find-your-job-api",
    image: $image,
    essential: true,
    portMappings: [{
      containerPort: 8000,
      hostPort: 8000,
      protocol: "tcp",
      appProtocol: "http"
    }],
    environment: [
      {name: "REQUIRE_DATABASE", value: "true"},
      {name: "EMBEDDING_PROVIDER", value: "jina"},
      {name: "JINA_EMBEDDING_MODEL", value: "jina-embeddings-v4"},
      {name: "DEEPSEEK_MODEL", value: "deepseek-v4-flash"},
      {name: "FRONTEND_ORIGINS", value: $frontendOrigins}
    ],
    secrets: [
      {name: "DATABASE_URL", valueFrom: $db},
      {name: "JINA_API_KEY", valueFrom: $jina},
      {name: "DEEPSEEK_API_KEY", valueFrom: $deepseek}
    ],
    logConfiguration: {
      logDriver: "awslogs",
      options: {
        "awslogs-group": "/ecs/ai-find-your-job-api",
        "awslogs-region": $region,
        "awslogs-stream-prefix": "ecs"
      }
    }
  }]')"

TASK_DEF_ARN="$(aws ecs register-task-definition \
  --family ai-find-your-job-api \
  --requires-compatibilities FARGATE \
  --network-mode awsvpc \
  --cpu 256 \
  --memory 512 \
  --execution-role-arn "$EXEC_ROLE_ARN" \
  --runtime-platform cpuArchitecture=X86_64,operatingSystemFamily=LINUX \
  --container-definitions "$CONTAINER_DEFINITIONS" \
  --region "$AWS_REGION" \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)"

aws ecs update-service \
  --cluster ai-find-your-job-cluster \
  --service ai-find-your-job-api-service \
  --task-definition "$TASK_DEF_ARN" \
  --force-new-deployment \
  --region "$AWS_REGION" >/dev/null

aws ecs wait services-stable \
  --cluster ai-find-your-job-cluster \
  --services ai-find-your-job-api-service \
  --region "$AWS_REGION"

echo "Updated ECS CORS origins:"
echo "$FRONTEND_ORIGINS"

if [ -f .deploy/api-cloudfront-url ]; then
  curl -fsS "$(cat .deploy/api-cloudfront-url)/health"
else
  curl -fsS "http://ai-find-your-job-alb-1660843417.${AWS_REGION}.elb.amazonaws.com/health"
fi
echo
