"""A generic 200 or an older deployment must never pass readiness."""

import base64
import json
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI

from modal_agents import checks

SECRET = "whsec_" + base64.b64encode(b"probe-test-key").decode()


def test_probe_signatures_verify_against_real_openai_sdk():
    body = b'{"type":"test"}'
    with OpenAI(api_key="unused", webhook_secret=SECRET) as client:
        client.webhooks.verify_signature(body.decode(), checks.signing_headers(body, SECRET))


@pytest.mark.parametrize("secret", ["", checks.PENDING_SIGNING_SECRET, "whsec_!!!", "whsec_"])
def test_probe_rejects_unconfigured_or_invalid_secret(secret):
    with pytest.raises(ValueError):
        checks.signing_headers(b"{}", secret)


@pytest.mark.parametrize(
    "behavior", ["ready", "old", "wrong_challenge", "unsigned_ok", "503", "bad_json", "network"]
)
def test_readiness_requires_authentication_and_current_handler(monkeypatch, behavior):
    def handle(request):
        assert request.url == "https://endpoint.modal.run/"
        if behavior == "network":
            raise httpx.ConnectError("sensitive transport detail")
        if behavior == "503":
            return httpx.Response(503)
        if "webhook-signature" not in request.headers:
            return httpx.Response(200 if behavior == "unsigned_ok" else 400)
        challenge = json.loads(request.content)["challenge"]
        if behavior == "bad_json":
            return httpx.Response(200, text="not json")
        data = {"status": "ready", "challenge": challenge, "version": checks.PROBE_VERSION}
        if behavior == "old":
            data = {"status": "ignored"}
        elif behavior == "wrong_challenge":
            data["challenge"] = "stale"
        return httpx.Response(200, json=data)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(checks.httpx, "Client", Mock(return_value=client))
    result = checks.probe_webhook("https://endpoint.modal.run/", SECRET, wait=0)
    assert result["status"] == ("ready" if behavior == "ready" else "not_ready")
    assert "sensitive" not in str(result)


def test_readiness_retries_stale_deployment(monkeypatch):
    attempts = []

    def handle(request):
        if "webhook-signature" not in request.headers:
            return httpx.Response(400)
        data = json.loads(request.content)
        attempts.append(data["challenge"])
        if len(attempts) == 1:
            return httpx.Response(200, json={"status": "ignored"})
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "challenge": data["challenge"],
                "version": checks.PROBE_VERSION,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(checks.httpx, "Client", Mock(return_value=client))
    monkeypatch.setattr(checks.time, "sleep", Mock())
    assert checks.probe_webhook("https://endpoint.modal.run/", SECRET)["status"] == "ready"
    assert len(set(attempts)) == 2
