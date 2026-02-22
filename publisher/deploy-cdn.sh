#!/usr/bin/env bash
# Deploy Nexflo publisher scripts to cdn.nexflo.ai (S3 + CloudFront)
#
# Prerequisites:
#   - AWS CLI configured (us-east-1, with s3:PutObject on nxflo-assets)
#   - prebid.js custom build at build/dist/prebid.js
#     (see nexflo-bid-adapter.js for build instructions)
#
# Usage:
#   ./publisher/deploy-cdn.sh [--invalidate]
#
set -euo pipefail

BUCKET="nxflo-assets"
REGION="us-east-1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Deploying publisher scripts to s3://${BUCKET}..."

# prebid-wrapper.js — short cache (24h) so updates propagate quickly
aws s3 cp "${SCRIPT_DIR}/prebid-wrapper.js" "s3://${BUCKET}/prebid-wrapper.js" \
  --content-type "application/javascript; charset=utf-8" \
  --cache-control "public, max-age=86400, s-maxage=86400" \
  --region "${REGION}"
echo "  ✓ prebid-wrapper.js"

# prebid.js — long cache (30d), version-pinned build
PREBID_BUILD="${SCRIPT_DIR}/../build/dist/prebid.js"
if [[ -f "${PREBID_BUILD}" ]]; then
  aws s3 cp "${PREBID_BUILD}" "s3://${BUCKET}/vendor/prebid.js" \
    --content-type "application/javascript; charset=utf-8" \
    --cache-control "public, max-age=2592000, s-maxage=2592000" \
    --region "${REGION}"
  echo "  ✓ vendor/prebid.js"
else
  echo "  ⚠ vendor/prebid.js skipped — build/dist/prebid.js not found"
  echo "    Build it first: see publisher/nexflo-bid-adapter.js for instructions"
fi

# Invalidate CloudFront if --invalidate flag passed
if [[ "${1:-}" == "--invalidate" ]]; then
  # Get distribution ID by origin domain
  DIST_ID=$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?Origins.Items[0].DomainName=='nxflo-assets.s3.amazonaws.com'].Id" \
    --output text --region "${REGION}" | head -1)

  if [[ -n "${DIST_ID}" ]]; then
    aws cloudfront create-invalidation \
      --distribution-id "${DIST_ID}" \
      --paths "/prebid-wrapper.js" "/vendor/prebid.js" \
      --region "${REGION}"
    echo "  ✓ CloudFront invalidation created for ${DIST_ID}"
  else
    echo "  ⚠ Could not find CloudFront distribution for nxflo-assets"
  fi
fi

echo ""
echo "Done. Verify:"
echo "  curl -I https://cdn.nexflo.ai/prebid-wrapper.js"
echo "  curl -I https://cdn.nexflo.ai/vendor/prebid.js"
