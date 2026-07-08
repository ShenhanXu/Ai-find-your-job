#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-$(aws configure get region || true)}"
AWS_REGION="${AWS_REGION:-us-west-2}"
ALB_DNS="${ALB_DNS:-ai-find-your-job-alb-1660843417.${AWS_REGION}.elb.amazonaws.com}"
COMMENT="${COMMENT:-ai-find-your-job-api-cloudfront}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd aws
require_cmd jq
require_cmd curl

mkdir -p .deploy

CACHE_POLICY_ID="$(aws cloudfront list-cache-policies \
  --type managed \
  --query "CachePolicyList.Items[?CachePolicy.CachePolicyConfig.Name=='Managed-CachingDisabled'].CachePolicy.Id | [0]" \
  --output text)"
ORIGIN_REQUEST_POLICY_ID="$(aws cloudfront list-origin-request-policies \
  --type managed \
  --query "OriginRequestPolicyList.Items[?OriginRequestPolicy.OriginRequestPolicyConfig.Name=='Managed-AllViewerExceptHostHeader'].OriginRequestPolicy.Id | [0]" \
  --output text)"

if [ -z "$CACHE_POLICY_ID" ] || [ "$CACHE_POLICY_ID" = "None" ]; then
  echo "Could not find CloudFront managed cache policy: Managed-CachingDisabled" >&2
  exit 1
fi

if [ -z "$ORIGIN_REQUEST_POLICY_ID" ] || [ "$ORIGIN_REQUEST_POLICY_ID" = "None" ]; then
  echo "Could not find CloudFront managed origin request policy: Managed-AllViewerExceptHostHeader" >&2
  exit 1
fi

existing="$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='${COMMENT}'] | [0].{Id:Id,DomainName:DomainName,Status:Status}" \
  --output json)"

distribution_id="$(echo "$existing" | jq -r '.Id // empty')"
domain_name="$(echo "$existing" | jq -r '.DomainName // empty')"

if [ -z "$distribution_id" ]; then
  config_file="$(mktemp)"
  trap 'rm -f "$config_file"' EXIT

  jq -n \
    --arg caller "ai-find-your-job-api-$(date +%s)" \
    --arg comment "$COMMENT" \
    --arg alb "$ALB_DNS" \
    --arg cachePolicyId "$CACHE_POLICY_ID" \
    --arg originRequestPolicyId "$ORIGIN_REQUEST_POLICY_ID" \
    '{
      CallerReference: $caller,
      Comment: $comment,
      Enabled: true,
      PriceClass: "PriceClass_100",
      HttpVersion: "http2",
      IsIPV6Enabled: true,
      Aliases: {Quantity: 0},
      Origins: {
        Quantity: 1,
        Items: [{
          Id: "ai-find-your-job-alb",
          DomainName: $alb,
          CustomOriginConfig: {
            HTTPPort: 80,
            HTTPSPort: 443,
            OriginProtocolPolicy: "http-only",
            OriginSslProtocols: {Quantity: 1, Items: ["TLSv1.2"]},
            OriginReadTimeout: 30,
            OriginKeepaliveTimeout: 5
          }
        }]
      },
      DefaultCacheBehavior: {
        TargetOriginId: "ai-find-your-job-alb",
        ViewerProtocolPolicy: "redirect-to-https",
        AllowedMethods: {
          Quantity: 7,
          Items: ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"],
          CachedMethods: {Quantity: 2, Items: ["GET", "HEAD"]}
        },
        Compress: true,
        CachePolicyId: $cachePolicyId,
        OriginRequestPolicyId: $originRequestPolicyId
      },
      ViewerCertificate: {CloudFrontDefaultCertificate: true},
      Restrictions: {
        GeoRestriction: {RestrictionType: "none", Quantity: 0}
      }
    }' > "$config_file"

  created="$(aws cloudfront create-distribution \
    --distribution-config "file://${config_file}" \
    --query 'Distribution.{Id:Id,DomainName:DomainName,Status:Status}' \
    --output json)"
  distribution_id="$(echo "$created" | jq -r '.Id')"
  domain_name="$(echo "$created" | jq -r '.DomainName')"
fi

api_url="https://${domain_name}"
printf "%s\n" "$api_url" > .deploy/api-cloudfront-url

echo "CloudFront distribution: ${distribution_id}"
echo "API HTTPS URL: ${api_url}"
echo "Waiting for CloudFront deployment. This can take several minutes."

aws cloudfront wait distribution-deployed --id "$distribution_id"

curl -fsS "${api_url}/health"
echo
echo "Saved API URL to .deploy/api-cloudfront-url"
