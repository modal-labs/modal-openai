"""Source templates for generated pool files."""

POOL_CONFIG = '''"""OpenAI Agents API pool configuration; wire into the webhook handler."""

import modal

OPENAI_SECRET_NAME = $secret_name
OPENAI_AGENT_ID = $agent_id
WORKER_SECRET_NAMES = ()

pool = {
    "name": $pool_name,
    "agent_id": OPENAI_AGENT_ID,
    "image": (
        modal.Image.debian_slim(python_version="3.13")
        .apt_install("git", "nodejs", "npm", "ripgrep")
        .run_commands("npm install -g @openai/codex@alpha")
    ),
    # "gpu": "A10G",
    # "cpu": 4,
    # "memory": 16384,
}
'''

WEBHOOK_HANDLER = '''"""Verify and log OpenAI webhooks. Sandbox lifecycle handling is not implemented."""

import json
import logging

import modal
from fastapi import HTTPException, Request
from openai import InvalidWebhookSignatureError, OpenAI

logger = logging.getLogger(__name__)
app = modal.App($secret_name)
image = modal.Image.debian_slim(python_version="3.13").pip_install(
    "fastapi>=0.115", "openai>=1.92"
)


@app.function(image=image, secrets=[modal.Secret.from_name($secret_name)])
@modal.fastapi_endpoint(method="POST")
async def webhook(request: Request) -> dict[str, str]:
    raw = await request.body()
    with OpenAI() as client:
        try:
            client.webhooks.verify_signature(raw, request.headers)
        except InvalidWebhookSignatureError as exc:
            raise HTTPException(status_code=400, detail="Invalid signature") from exc

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        raise HTTPException(status_code=400, detail="Event type must be a string")

    event_type = payload["type"]
    logger.info("Received %s event", event_type)
    # TODO: Start sandboxes on agent.session.action_required and clean up on failure.
    return {"status": "ok", "event_type": event_type}
'''

EXECUTOR_WRAPPER = '''#!/usr/bin/env bash
set -eu

ENVIRONMENT_ID="${1:?Usage: executor.sh ENVIRONMENT_ID}"
export CODEX_API_KEY="${OPENAI_EXECUTOR_API_KEY:?Must set OPENAI_EXECUTOR_API_KEY}"

mkdir -p /workspace
cd /workspace

exec codex exec-server \\
    --remote https://api.openai.com/v1/agents/api \\
    --environment-id "$ENVIRONMENT_ID"
'''
