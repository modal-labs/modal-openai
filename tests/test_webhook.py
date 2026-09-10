"""Real signature verification with mocked durable dispatch."""

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from modal_agents.server import MAX_WEBHOOK_BYTES, receive_webhook

SIGNING_KEY = b"test-signing-key"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPENAI_WEBHOOK_SECRET", "whsec_" + base64.b64encode(SIGNING_KEY).decode())
    # Signature verification never needs the application key.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    queue = AsyncMock()
    app = FastAPI()

    @app.post("/")
    async def webhook(request: Request):
        return await receive_webhook(request, queue)

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, queue


def signed_headers(body: bytes, *, timestamp: int | None = None) -> dict[str, str]:
    timestamp = int(time.time()) if timestamp is None else timestamp
    message = f"test-event.{timestamp}.".encode() + body
    signature = base64.b64encode(hmac.digest(SIGNING_KEY, message, hashlib.sha256)).decode()
    return {
        "webhook-id": "test-event",
        "webhook-timestamp": str(timestamp),
        "webhook-signature": f"v1,{signature}",
        "content-type": "application/json",
    }


def event(kind="agent.session.action_required", **data):
    return json.dumps(
        {
            "type": kind,
            "data": {"id": "session_test", "required_action": {"type": "environment_connection"}}
            | data,
        }
    ).encode()


@pytest.mark.parametrize("kind", ["agent.session.action_required", "agent.session.failed"])
def test_valid_signed_event_enqueues(client, kind):
    browser, queue = client
    body = event(kind)
    response = browser.post("/", content=body, headers=signed_headers(body))
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    queue.assert_awaited_once_with("session_test")


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff",
        b"[]",
        b"null",
        b"{}",
        b'{"type": 1}',
        b'{"type":"agent.session.failed"}',
        event(id="../outside"),
        event(required_action=None),
    ],
)
def test_signed_invalid_payload_is_rejected(client, body):
    response = client[0].post("/", content=body, headers=signed_headers(body))
    assert response.status_code == 400
    client[1].assert_not_awaited()


def test_tampered_payload_is_rejected(client):
    response = client[0].post("/", content=event(), headers=signed_headers(b"{}"))
    assert response.status_code == 400
    client[1].assert_not_awaited()


def test_expired_signature_is_rejected(client):
    body = event()
    response = client[0].post("/", content=body, headers=signed_headers(body, timestamp=1))
    assert response.status_code == 400


def test_unsigned_payload_is_rejected(client):
    assert client[0].post("/", content=event()).status_code == 400


@pytest.mark.parametrize(
    "body", [event("unrelated.event"), event(required_action={"type": "function_call"})]
)
def test_unrelated_events_are_acknowledged_without_work(client, body):
    response = client[0].post("/", content=body, headers=signed_headers(body))
    assert response.json() == {"status": "ignored"}
    client[1].assert_not_awaited()


@pytest.mark.parametrize("secret", ["", "pending-webhook-registration"])
def test_unconfigured_endpoint_returns_retryable_error(client, monkeypatch, secret):
    monkeypatch.setenv("OPENAI_WEBHOOK_SECRET", secret)
    assert client[0].post("/", content=event()).status_code == 503
    client[1].assert_not_awaited()


def test_queue_failure_is_not_acknowledged(client):
    client[1].side_effect = RuntimeError("queue unavailable")
    body = event()
    assert client[0].post("/", content=body, headers=signed_headers(body)).status_code == 500


def test_oversized_body_is_rejected(client):
    assert client[0].post("/", content=b"x" * (MAX_WEBHOOK_BYTES + 1)).status_code == 413
    client[1].assert_not_awaited()


def test_signed_readiness_challenge_never_enqueues(client):
    body = json.dumps({"type": "modal_openai.readiness", "challenge": "a" * 32}).encode()
    response = client[0].post("/", content=body, headers=signed_headers(body))
    assert response.json() == {"status": "ready", "challenge": "a" * 32, "version": "1"}
    client[1].assert_not_awaited()


@pytest.mark.parametrize("challenge", [None, "", "unexpected", "z" * 32, 42])
def test_readiness_rejects_malformed_challenge(client, challenge):
    body = json.dumps({"type": "modal_openai.readiness", "challenge": challenge}).encode()
    assert client[0].post("/", content=body, headers=signed_headers(body)).status_code == 400
    client[1].assert_not_awaited()
