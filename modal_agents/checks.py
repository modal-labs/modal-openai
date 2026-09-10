"""Signed readiness probes; no agent session or sandbox is created."""

import base64
import hashlib
import hmac
import json
import time
import uuid
from http import HTTPStatus

import httpx

PENDING_SIGNING_SECRET = "pending-webhook-registration"
PROBE_TYPE = "modal_openai.readiness"
PROBE_VERSION = "1"


def signing_headers(body: bytes, secret: str) -> dict[str, str]:
    """Sign the exact bytes using the OpenAI webhook signature format."""
    if not secret or secret == PENDING_SIGNING_SECRET:
        raise ValueError("Webhook signing secret is not configured")
    key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    if not key:
        raise ValueError("Webhook signing secret is empty")
    event_id = f"probe_{uuid.uuid4().hex}"
    timestamp = str(int(time.time()))
    signature = base64.b64encode(
        hmac.digest(key, f"{event_id}.{timestamp}.".encode() + body, hashlib.sha256)
    ).decode()
    return {
        "content-type": "application/json",
        "webhook-id": event_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": f"v1,{signature}",
    }


def probe_webhook(endpoint: str, secret: str, *, wait: float = 60) -> dict[str, str]:
    """Retry stale/unhealthy deployments, requiring both rejection and a signed challenge."""
    deadline = time.monotonic() + wait
    detail = "Endpoint did not respond"
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        while True:
            challenge = uuid.uuid4().hex
            body = json.dumps({"type": PROBE_TYPE, "challenge": challenge}).encode()
            headers = signing_headers(body, secret)
            try:
                unsigned = client.post(endpoint, content=body)
                signed = client.post(endpoint, content=body, headers=headers)
                expected = {"status": "ready", "challenge": challenge, "version": PROBE_VERSION}
                if (
                    unsigned.status_code == HTTPStatus.BAD_REQUEST
                    and signed.status_code == HTTPStatus.OK
                ):
                    if signed.json() == expected:
                        return {"status": "ready", "endpoint": endpoint}
                    detail = "Endpoint is serving an older or unexpected handler"
                else:
                    detail = (
                        f"Unsigned HTTP {unsigned.status_code}; signed HTTP {signed.status_code}"
                    )
            except (httpx.HTTPError, ValueError) as error:
                # Response bodies and transport errors can contain sensitive data.
                detail = f"Probe failed ({type(error).__name__})"
            if time.monotonic() >= deadline:
                return {"status": "not_ready", "detail": detail}
            time.sleep(min(2, max(0, deadline - time.monotonic())))
