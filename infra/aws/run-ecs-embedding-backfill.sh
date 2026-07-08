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

CLUSTER_NAME="${CLUSTER_NAME:-ai-find-your-job-cluster}"
TASK_FAMILY="${TASK_FAMILY:-ai-find-your-job-embedding-backfill}"
CONTAINER_NAME="${CONTAINER_NAME:-embedding-backfill}"
LOG_GROUP="${LOG_GROUP:-/ecs/ai-find-your-job-api}"
LOG_PREFIX="${LOG_PREFIX:-backfill}"
BACKFILL_LIMIT="${EMBEDDING_BACKFILL_LIMIT:-1000}"
BACKFILL_DELAY="${EMBEDDING_BACKFILL_DELAY_SECONDS:-0.2}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/ai-find-your-job-api:latest"
EXEC_ROLE_ARN="$(aws iam get-role --role-name ecsTaskExecutionRole --query 'Role.Arn' --output text)"

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

POLICY_DOC="$(jq -n \
  --arg db "$DB_SECRET_ARN" \
  --arg jina "$JINA_SECRET_ARN" \
  '{
    Version: "2012-10-17",
    Statement: [{
      Effect: "Allow",
      Action: ["secretsmanager:GetSecretValue"],
      Resource: [$db, $jina]
    }]
  }')"

aws iam put-role-policy \
  --role-name ecsTaskExecutionRole \
  --policy-name ai-find-your-job-read-backfill-secrets \
  --policy-document "$POLICY_DOC"

CONTAINER_DEFINITIONS="$(jq -n \
  --arg name "$CONTAINER_NAME" \
  --arg image "$IMAGE_URI" \
  --arg region "$AWS_REGION" \
  --arg logGroup "$LOG_GROUP" \
  --arg logPrefix "$LOG_PREFIX" \
  --arg db "$DB_SECRET_ARN" \
  --arg jina "$JINA_SECRET_ARN" \
  --arg limit "$BACKFILL_LIMIT" \
  --arg delay "$BACKFILL_DELAY" \
  '[{
    name: $name,
    image: $image,
    essential: true,
    command: ["python", "-m", "app.backfill_embeddings"],
    environment: [
      {name: "REQUIRE_DATABASE", value: "true"},
      {name: "EMBEDDING_PROVIDER", value: "jina"},
      {name: "JINA_EMBEDDING_MODEL", value: "jina-embeddings-v4"},
      {name: "EMBEDDING_DIMENSIONS", value: "1536"},
      {name: "EMBEDDING_BACKFILL_LIMIT", value: $limit},
      {name: "EMBEDDING_BACKFILL_DELAY_SECONDS", value: $delay}
    ],
    secrets: [
      {name: "DATABASE_URL", valueFrom: $db},
      {name: "JINA_API_KEY", valueFrom: $jina}
    ],
    logConfiguration: {
      logDriver: "awslogs",
      options: {
        "awslogs-group": $logGroup,
        "awslogs-region": $region,
        "awslogs-stream-prefix": $logPrefix
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

VPC_ID="$(aws ec2 describe-vpcs \
  --filters Name=is-default,Values=true \
  --region "$AWS_REGION" \
  --query 'Vpcs[0].VpcId' \
  --output text)"
read -r -a SUBNET_ARRAY <<< "$(aws ec2 describe-subnets \
  --filters Name=vpc-id,Values="$VPC_ID" Name=default-for-az,Values=true \
  --region "$AWS_REGION" \
  --query 'Subnets[].SubnetId' \
  --output text)"
ECS_SG="$(aws ec2 describe-security-groups \
  --filters Name=vpc-id,Values="$VPC_ID" Name=group-name,Values=ai-find-your-job-api-sg \
  --region "$AWS_REGION" \
  --query 'SecurityGroups[0].GroupId' \
  --output text)"

TASK_ARN="$(aws ecs run-task \
  --cluster "$CLUSTER_NAME" \
  --task-definition "$TASK_DEF_ARN" \
  --launch-type FARGATE \
  --platform-version LATEST \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNET_ARRAY[0]},${SUBNET_ARRAY[1]},${SUBNET_ARRAY[2]},${SUBNET_ARRAY[3]}],securityGroups=[${ECS_SG}],assignPublicIp=ENABLED}" \
  --region "$AWS_REGION" \
  --query 'tasks[0].taskArn' \
  --output text)"

TASK_ID="${TASK_ARN##*/}"
echo "Started embedding backfill task: ${TASK_ID}"

aws ecs wait tasks-stopped \
  --cluster "$CLUSTER_NAME" \
  --tasks "$TASK_ARN" \
  --region "$AWS_REGION"

TASK_INFO="$(aws ecs describe-tasks \
  --cluster "$CLUSTER_NAME" \
  --tasks "$TASK_ARN" \
  --region "$AWS_REGION" \
  --query 'tasks[0].containers[0].{lastStatus:lastStatus,exitCode:exitCode,reason:reason}' \
  --output json)"

echo "$TASK_INFO"

LOG_STREAM="${LOG_PREFIX}/${CONTAINER_NAME}/${TASK_ID}"
echo "CloudWatch log stream: ${LOG_STREAM}"
aws logs get-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$LOG_STREAM" \
  --region "$AWS_REGION" \
  --query 'events[].message' \
  --output text || true
echo

EXIT_CODE="$(echo "$TASK_INFO" | jq -r '.exitCode // empty')"
if [ "$EXIT_CODE" != "0" ]; then
  echo "Embedding backfill failed with exit code ${EXIT_CODE:-unknown}." >&2
  exit 1
fi

echo "Embedding backfill completed successfully."
