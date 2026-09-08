#!/usr/bin/env bash
set -euo pipefail

ENVIRONMENT_ID="${1:?Usage: executor.sh ENVIRONMENT_ID}"
: "${CODEX_API_KEY:?Must set CODEX_API_KEY to the restricted executor key}"
WORKSPACE="${MODAL_AGENTS_WORKSPACE:-/workspace}"
mkdir -p "$WORKSPACE"
cd "$WORKSPACE"

exec codex exec-server \
    --remote https://api.openai.com/v1/agents/api \
    --environment-id "$ENVIRONMENT_ID"
