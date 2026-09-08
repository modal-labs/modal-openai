"""OpenAI Agents API webhook handler for sandbox lifecycle management."""

from __future__ import annotations

import json
import logging
import os

import modal
from fastapi import HTTPException, Request
from openai import InvalidWebhookSignatureError, OpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = modal.App("openai-agents-test-agent")

webhook_image = (
    modal.Image.debian_slim(python_version="3.13")
    .pip_install(
        "fastapi>=0.115.0",
        "openai>=1.92.0",
    )
)

secrets = modal.Secret.from_dict(
    {
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
        "OPENAI_WEBHOOK_SECRET": os.environ.get("OPENAI_WEBHOOK_SECRET", ""),
    }
)


def verify_webhook_signature(raw: bytes, headers: dict[str, str]) -> bool:
    """Verify OpenAI webhook signature."""
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        webhook_secret=os.environ["OPENAI_WEBHOOK_SECRET"],
    )
    try:
        client.webhooks.verify_signature(raw, headers)
        return True
    except InvalidWebhookSignatureError:
        return False


@app.function(image=webhook_image, secrets=[secrets], timeout=600)
@modal.fastapi_endpoint(method="POST")
async def webhook(request: Request) -> dict[str, str]:
    """Receive and handle OpenAI webhook events."""
    raw = await request.body()
    headers = {k: v for k, v in request.headers.items()}

    if not verify_webhook_signature(raw, headers):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("type")
    logger.info(f"Received {event_type} event")

    # TODO: Implement event handling
    # - agent.session.created: Start sandbox
    # - agent.session.failed: Cleanup sandbox

    return {"status": "ok", "event_type": event_type}
