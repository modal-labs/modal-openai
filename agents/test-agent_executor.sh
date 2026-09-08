#!/usr/bin/env bash
# Simple wrapper to start the executor in a Modal sandbox

set -e

ENVIRONMENT_ID="$1"
OPENAI_EXECUTOR_API_KEY="${OPENAI_EXECUTOR_API_KEY:?Must set OPENAI_EXECUTOR_API_KEY}"

mkdir -p /workspace
cd /workspace

exec codex exec-server \
    --remote https://api.openai.com/v1/agents/api \
    --environment-id "$ENVIRONMENT_ID"
