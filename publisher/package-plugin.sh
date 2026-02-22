#!/usr/bin/env bash
# Package the Nexflo WordPress plugin for submission to wordpress.org
#
# Usage:
#   ./publisher/package-plugin.sh
#
# Output: publisher/nexflo-ads.zip
# Submit at: https://wordpress.org/plugins/developers/add/
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="${SCRIPT_DIR}/wordpress-plugin"
OUTPUT="${SCRIPT_DIR}/nexflo-ads.zip"

if [[ -f "${OUTPUT}" ]]; then
  rm "${OUTPUT}"
fi

cd "${SCRIPT_DIR}"
zip -r nexflo-ads.zip wordpress-plugin/ \
  --exclude "*.DS_Store" \
  --exclude "*/__pycache__/*" \
  --exclude "*.pyc"

echo "Created: ${OUTPUT}"
echo ""
echo "Submit to wordpress.org:"
echo "  https://wordpress.org/plugins/developers/add/"
echo ""
echo "Checklist before submitting:"
echo "  [ ] Add screenshots to wordpress-plugin/assets/ (screenshot-1.png, screenshot-2.png)"
echo "  [ ] Test on a local WordPress install (wp-env or LocalWP)"
echo "  [ ] Confirm plugin activates without PHP errors"
echo "  [ ] Confirm Settings → Nexflo Ads page loads"
echo "  [ ] Confirm script tag appears in <head> after saving site ID"
