#!/usr/bin/env bash
# Build .difypkg package for Dify AgenTrux Trigger plugin.
# Mirror of plugins/dify-agentrux-tools/scripts/build.sh — keep these two in sync.
set -euo pipefail

for cmd in zip python3; do
  command -v "$cmd" >/dev/null || { echo "Error: $cmd is required"; exit 1; }
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"
PLUGIN_NAME="dify-agentrux-trigger"

VERSION=$(grep '^version:' "$PLUGIN_DIR/manifest.yaml" | awk '{print $2}')
OUTPUT="${PLUGIN_DIR}/dist/${PLUGIN_NAME}-${VERSION}.difypkg"

echo "Building ${PLUGIN_NAME} v${VERSION}..."

mkdir -p "$PLUGIN_DIR/dist"
rm -f "$OUTPUT"

cd "$PLUGIN_DIR"
find . -type f \
  ! -path './dist/*' \
  ! -path './scripts/*' \
  ! -path './.DS_Store' \
  ! -name '*.pyc' \
  ! -path '*__pycache__*' \
  | zip "$OUTPUT" -@

echo ""
echo "Built: $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
echo ""
echo "Install in Dify:"
echo "  1. Open Dify → Plugins → Install from local file"
echo "  2. Select: $OUTPUT"
