#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-$(aws configure get region || true)}"
AWS_REGION="${AWS_REGION:-us-west-2}"
CLUSTER_NAME="${CLUSTER_NAME:-ai-find-your-job-cluster}"
SERVICE_NAME="${SERVICE_NAME:-ai-find-your-job-api-service}"
TASK_FAMILY="${TASK_FAMILY:-ai-find-your-job-api}"
REPOSITORY_NAME="${REPOSITORY_NAME:-ai-find-your-job-api}"
CONTAINER_NAME="${CONTAINER_NAME:-ai-find-your-job-api}"
LOG_GROUP="${LOG_GROUP:-/ecs/ai-find-your-job-api}"
FRONTEND_ORIGIN="${1:-${FRONTEND_ORIGIN:-}}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd aws
require_cmd curl
require_cmd docker
require_cmd jq

if [ -z "$FRONTEND_ORIGIN" ] && [ -f .deploy/vercel-url ]; then
  FRONTEND_ORIGIN="$(cat .deploy/vercel-url)"
fi

if [[ "${FRONTEND_ORIGIN}" =~ ^(https?://[^/]+) ]]; then
  FRONTEND_ORIGIN="${BASH_REMATCH[1]}"
fi
FRONTEND_ORIGIN="${FRONTEND_ORIGIN%/}"

if [ -n "$FRONTEND_ORIGIN" ]; then
  FRONTEND_ORIGINS="${FRONTEND_ORIGINS:-http://localhost:3000,http://127.0.0.1:3000,${FRONTEND_ORIGIN}}"
else
  FRONTEND_ORIGINS="${FRONTEND_ORIGINS:-http://localhost:3000,http://127.0.0.1:3000}"
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE_REPO="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPOSITORY_NAME}"
IMAGE_TAG="${IMAGE_TAG:-$(date +%Y%m%d%H%M%S)}"
IMAGE_URI="${IMAGE_REPO}:${IMAGE_TAG}"

if ! aws ecr describe-repositories --repository-names "$REPOSITORY_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  aws ecr create-repository \
    --repository-name "$REPOSITORY_NAME" \
    --region "$AWS_REGION" >/dev/null
fi

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com" >/dev/null

docker buildx build \
  --platform linux/amd64 \
  -f apps/api/Dockerfile \
  -t "$IMAGE_URI" \
  -t "${IMAGE_REPO}:latest" \
  --push \
  .

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
EXEC_ROLE_ARN="$(aws iam get-role --role-name ecsTaskExecutionRole --query 'Role.Arn' --output text)"

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
  --policy-document "$POLICY_DOC" >/dev/null

CONTAINER_DEFINITIONS="$(jq -n \
  --arg image "$IMAGE_URI" \
  --arg region "$AWS_REGION" \
  --arg frontendOrigins "$FRONTEND_ORIGINS" \
  --arg db "$DB_SECRET_ARN" \
  --arg jina "$JINA_SECRET_ARN" \
  --arg deepseek "$DEEPSEEK_SECRET_ARN" \
  --arg logGroup "$LOG_GROUP" \
  --arg containerName "$CONTAINER_NAME" \
  '[{
    name: $containerName,
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
        "awslogs-group": $logGroup,
        "awslogs-region": $region,
        "awslogs-stream-prefix": "ecs"
      }
    }
  }]')"

TASK_DEF_ARN="$(aws ecs register-task-definition \
  --family "$TASK_FAMILY" \
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
  --cluster "$CLUSTER_NAME" \
  --service "$SERVICE_NAME" \
  --task-definition "$TASK_DEF_ARN" \
  --force-new-deployment \
  --region "$AWS_REGION" >/dev/null

aws ecs wait services-stable \
  --cluster "$CLUSTER_NAME" \
  --services "$SERVICE_NAME" \
  --region "$AWS_REGION"

if [ -f .deploy/api-cloudfront-url ]; then
  API_URL="$(cat .deploy/api-cloudfront-url)"
else
  API_URL="http://ai-find-your-job-alb-1660843417.${AWS_REGION}.elb.amazonaws.com"
fi

echo "Deployed image: $IMAGE_URI"
echo "Health:"
curl -fsS "${API_URL}/health"
echo
echo "Internal trace smoke test:"
curl -fsS "${API_URL}/match" \
  -H 'Content-Type: application/json' \
  -H 'x-jobtrace-monitoring: internal' \
  -H 'x-jobtrace-internal-steps: workflow_total,select_jobs,match_resume,sort_results' \
  -d '{
    "resume": {
      "content": "Seattle backend software engineer with Java Spring Boot AWS Redis PostgreSQL Docker FastAPI React TypeScript observability tracing and distributed systems experience building production APIs and dashboards.",
      "target_role": "Seattle Backend SDE"
    },
    "use_ai": false
  }' \
  | jq '{has_workflow_trace: has("workflow_trace"), steps: (.workflow_trace.steps // [] | map(.name)), result_count: (.results // [] | length)}'
