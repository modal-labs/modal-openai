"""Signature verification and deployment wiring for the webhook worker."""

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from pathlib import Path
from typing import cast

import modal
from fastapi import HTTPException, Request
from openai import InvalidWebhookSignatureError, OpenAI
from opentelemetry.trace import SpanKind

from modal_agents.checks import PENDING_SIGNING_SECRET, PROBE_TYPE, PROBE_VERSION, probe_webhook
from modal_agents.pool import Pool
from modal_agents.runtime import reconcile_session
from modal_agents.telemetry import (
    continue_trace,
    flush_telemetry,
    inject_trace_context,
    new_root_span,
    set_attribute,
    span,
    telemetry_environment,
)

MAX_WEBHOOK_BYTES = 1024 * 1024


def event_session_id(payload: object) -> str | None:
    """Validate only events this integration can act on."""
    if not isinstance(payload, dict):
        raise ValueError("Event must be an object")
    payload = cast(dict[str, object], payload)
    if not isinstance(payload.get("type"), str):
        raise ValueError("Event type must be a string")
    if payload["type"] not in {"agent.session.action_required", "agent.session.failed"}:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Event data must be an object")
    data = cast(dict[str, object], data)
    if payload["type"] == "agent.session.action_required":
        action = data.get("required_action")
        if not isinstance(action, dict):
            raise ValueError("Event must contain a required_action object")
        action = cast(dict[str, object], action)
        if not isinstance(action.get("type"), str):
            raise ValueError("Event must contain a required_action type")
        if action["type"] != "environment_connection":
            return None
    session_id = data.get("id")
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
        raise ValueError("Event must contain a valid session ID")
    return session_id


async def receive_webhook(
    request: Request, enqueue: Callable[[str], Awaitable[None]]
) -> dict[str, str]:
    try:
        with (
            continue_trace(request.headers),
            span(
                "modal_openai.webhook.receive",
                {"http.request.method": "POST"},
                kind=SpanKind.SERVER,
            ) as current,
        ):
            try:
                result = await _receive_webhook(request, enqueue)
            except HTTPException as error:
                set_attribute(current, "http.response.status_code", error.status_code)
                raise
            except Exception:
                set_attribute(current, "http.response.status_code", 500)
                raise
            set_attribute(current, "http.response.status_code", 200)
            set_attribute(current, "modal_openai.outcome", result["status"])
            return result
    finally:
        # Close all spans before flushing; exporter I/O stays off the event loop.
        await asyncio.to_thread(flush_telemetry)


async def _receive_webhook(
    request: Request, enqueue: Callable[[str], Awaitable[None]]
) -> dict[str, str]:
    secret = os.environ.get("OPENAI_WEBHOOK_SECRET")
    if not secret or secret == PENDING_SIGNING_SECRET:
        raise HTTPException(status_code=503, detail="Webhook signing secret is not configured")

    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Webhook body is too large")
    try:
        text = raw.decode("utf-8")
        with (
            span("modal_openai.webhook.verify"),
            OpenAI(api_key="unused", webhook_secret=secret) as verifier,
        ):
            verifier.webhooks.verify_signature(text, request.headers)
    except (InvalidWebhookSignatureError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Invalid webhook signature") from error
    try:
        with span("modal_openai.webhook.parse"):
            payload = json.loads(text)
            session_id = event_session_id(payload)
            if payload.get("type") == PROBE_TYPE:
                challenge = payload.get("challenge")
                if not isinstance(challenge, str) or not re.fullmatch(r"[a-f0-9]{32}", challenge):
                    raise ValueError("Invalid readiness challenge")
                return {"status": "ready", "challenge": challenge, "version": PROBE_VERSION}
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid webhook event") from error
    if session_id is None:
        return {"status": "ignored"}
    # Acknowledge after Modal accepts the call. Queue failures remain retryable.
    with span("modal_openai.webhook.enqueue", {"gen_ai.conversation.id": session_id}):
        await enqueue(session_id)
    return {"status": "queued"}


def build_app(pool: Pool, executor_path: Path) -> modal.App:
    """Create a fast endpoint and one serialized reconciliation worker per pool."""
    app = modal.App(pool.app_name)
    executor_image = pool.image.add_local_file(
        executor_path, "/opt/modal-agents/executor.sh", copy=True
    )
    controller_image = executor_image.pip_install(
        *(
            f"{name}=={version(name)}"
            for name in ("fastapi", "openai", "httpx", "pydantic", "opentelemetry-api")
        ),
        f"logfire[httpx]=={version('logfire')}",
    ).add_local_python_source("modal_agents")
    # Exporter URLs/headers may contain credentials. Pass them as a Modal Secret.
    # Do not forward the deploy process's traceparent into long-lived containers.
    telemetry_secrets: list[modal.Secret] = []
    if settings := telemetry_environment():
        telemetry_secrets.append(modal.Secret.from_dict({**settings}))

    @app.function(  # pyright: ignore[reportUnknownMemberType]
        image=controller_image,
        serialized=True,
        secrets=[
            modal.Secret.from_name(pool.controller_secret, required_keys=["OPENAI_API_KEY"]),
            *telemetry_secrets,
        ],
        name="reconcile",
        max_containers=1,
        timeout=180,
        retries=3,
    )
    async def reconcile(session_id: str, trace_carrier: dict[str, str] | None = None) -> str:
        try:
            with new_root_span(
                trace_carrier or {},
                "modal_openai.worker.reconcile",
                **{
                    "modal_openai.pool.name": pool.name,
                    "gen_ai.conversation.id": session_id,
                },
            ) as current:
                # Resolve at runtime: a deployed App holds an unpicklable live client.
                worker_app = await modal.App.lookup.aio(pool.app_name)
                outcome = await reconcile_session(pool, worker_app, executor_image, session_id)
                set_attribute(current, "modal_openai.outcome", outcome)
                return outcome
        finally:
            await asyncio.to_thread(flush_telemetry)

    @app.function(  # pyright: ignore[reportUnknownMemberType]
        image=controller_image,
        serialized=True,
        secrets=[
            modal.Secret.from_name(pool.signing_secret, required_keys=["OPENAI_WEBHOOK_SECRET"]),
            *telemetry_secrets,
        ],
        name="webhook",
        timeout=30,
    )
    @modal.fastapi_endpoint(method="POST")  # pyright: ignore[reportUnknownMemberType]
    async def webhook(request: Request) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        async def enqueue(session_id: str) -> None:
            carrier: dict[str, str] = {}
            inject_trace_context(carrier)
            await reconcile.spawn.aio(session_id, carrier)

        return await receive_webhook(request, enqueue)

    @app.function(  # pyright: ignore[reportUnknownMemberType]
        image=controller_image,
        serialized=True,
        secrets=[modal.Secret.from_name(pool.signing_secret), *telemetry_secrets],
        name="check_webhook",
        timeout=120,
    )
    def check_webhook(endpoint: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return probe_webhook(endpoint, os.environ.get("OPENAI_WEBHOOK_SECRET", ""))

    return app
