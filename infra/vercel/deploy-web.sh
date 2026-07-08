#!/usr/bin/env bash
set -euo pipefail

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd npm

API_URL="${NEXT_PUBLIC_API_URL:-${1:-}}"
if [ -z "$API_URL" ] && [ -f .deploy/api-cloudfront-url ]; then
  API_URL="$(cat .deploy/api-cloudfront-url)"
fi

if [ -z "$API_URL" ]; then
  echo "Missing API URL. Run ./infra/aws/create-api-cloudfront.sh first, or pass the API URL:" >&2
  echo "./infra/vercel/deploy-web.sh https://example.cloudfront.net" >&2
  exit 1
fi

API_URL="${API_URL%/}"
mkdir -p .deploy

echo "Deploying apps/web to Vercel."
echo "NEXT_PUBLIC_API_URL=${API_URL}"

output_file="$(mktemp)"
trap 'rm -f "$output_file"' EXIT

npx vercel@latest \
  --cwd apps/web \
  --prod \
  --yes \
  --build-env "NEXT_PUBLIC_API_URL=${API_URL}" \
  --env "NEXT_PUBLIC_API_URL=${API_URL}" | tee "$output_file"

deployment_url="$(grep -Eo 'https://[^ ]+\.vercel\.app' "$output_file" | tail -n 1 || true)"
if [ -z "$deployment_url" ]; then
  echo "Could not parse the Vercel deployment URL from the CLI output." >&2
  echo "Copy the production Vercel URL manually, then run:" >&2
  echo "./infra/aws/update-ecs-frontend-origin.sh https://your-project.vercel.app" >&2
  exit 1
fi

deployment_url="${deployment_url%/}"
printf "%s\n" "$deployment_url" > .deploy/vercel-url

echo "Vercel URL: ${deployment_url}"
echo "Saved Vercel URL to .deploy/vercel-url"
echo "Next: ./infra/aws/update-ecs-frontend-origin.sh"
