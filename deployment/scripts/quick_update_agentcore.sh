#!/bin/bash
# Quick update wrapper for the canonical Python build/push/runtime-refresh flow.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$0")/../.."

echo "========================================"
echo "Quick AgentCore Update"
echo "========================================"
echo ""

python3 deployment/scripts/build_and_push_image.py "$@"
