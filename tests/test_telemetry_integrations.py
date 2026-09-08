"""Verify lifecycle call sites export useful, bounded, credential-free traces."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import modal
import pytest
from fastapi import HTTPException
from opentelemetry.trace import SpanKind, StatusCode
from starlette.requests import Request

from modal_agents import cli, runtime, server, telemetry
from modal_agents.pool import Pool


def spans(capfire):
    return {
        item.name: item
        for item in capfire.exporter.exported_spans
        if item.attributes.get("logfire.span_type") == "span"
    }


def request_for(payload, carrier=None):
    body = json.dumps(payload).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(key.encode(), value.encode()) for key, value in (carrier or {}).items()],
        },
        receive,
    )


@pytest.fixture
def verifier(monkeypatch):
    monkeypatch.setenv("OPENAI_WEBHOOK_SECRET", "signing-sentinel")
    verifier = Mock()
    verifier.__enter__ = Mock(return_value=verifier)
    verifier.__exit__ = Mock(return_value=None)
    monkeypatch.setattr(server, "OpenAI", Mock(return_value=verifier))
    return verifier


def test_webhook_continues_parent_and_flushes_after_enqueue(capfire, monkeypatch, verifier):
    carrier = {}
    with telemetry.span("upstream"):
        telemetry.inject_trace_context(carrier)
    flush_names = []
    monkeypatch.setattr(server, "flush_telemetry", lambda: flush_names.extend(spans(capfire)))
    enqueue = AsyncMock()
    result = asyncio.run(
        server.receive_webhook(
            request_for(
                {
                    "type": "agent.session.failed",
                    "data": {"id": "session_test", "prompt": "body-sentinel"},
                },
                carrier,
            ),
            enqueue,
        )
    )
    assert result == {"status": "queued"}
    enqueue.assert_awaited_once_with("session_test")
    exported = spans(capfire)
    incoming = exported["modal_openai.webhook.receive"]
    assert incoming.parent.span_id == exported["upstream"].context.span_id
    assert incoming.kind == SpanKind.SERVER
    assert incoming.attributes["http.response.status_code"] == 200
    assert incoming.attributes["modal_openai.outcome"] == "queued"
    assert exported["modal_openai.webhook.enqueue"].parent.span_id == incoming.context.span_id
    assert "modal_openai.webhook.receive" in flush_names
    assert "body-sentinel" not in repr(exported)
    assert "signing-sentinel" not in repr(exported)


def test_webhook_rejection_is_traced_and_flushed(capfire, monkeypatch, verifier):
    verifier.webhooks.verify_signature.side_effect = ValueError("signature-sentinel")
    flush = Mock()
    monkeypatch.setattr(server, "flush_telemetry", flush)
    enqueue = AsyncMock()
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.receive_webhook(request_for({}), enqueue))
    assert error.value.status_code == 400
    incoming = spans(capfire)["modal_openai.webhook.receive"]
    assert incoming.attributes["http.response.status_code"] == 400
    assert incoming.status.status_code == StatusCode.ERROR
    assert "signature-sentinel" not in repr(spans(capfire))
    enqueue.assert_not_awaited()
    flush.assert_called_once()


def test_reconciliation_and_http_client_share_trace_without_payloads(capfire, monkeypatch):
    captured_headers = []
    session = {
        "id": "session_test",
        "agent": {"id": "agent_test"},
        "status": "pending",
        "environment": {"type": "self_hosted", "id": "env_test"},
        "required_actions": [{"type": "environment_connection", "environment_id": "env_test"}],
        "prompt": "response-body-sentinel",
    }

    def respond(request):
        captured_headers.append(request.headers)
        return httpx.Response(200, json=session)

    monkeypatch.setattr(
        runtime,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: httpx.AsyncClient(
                transport=httpx.MockTransport(respond), **kwargs
            )
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "api-key-sentinel")
    monkeypatch.setattr(runtime, "find_sandbox", AsyncMock(return_value=None))
    sandbox = SimpleNamespace(object_id="sb_test", detach=SimpleNamespace(aio=AsyncMock()))
    monkeypatch.setattr(
        runtime.modal.Sandbox,
        "create",
        SimpleNamespace(aio=AsyncMock(return_value=sandbox)),
    )
    image = modal.Image.debian_slim()
    pool = Pool(name="test", agent_id="agent_test", image=image)
    result = asyncio.run(runtime.reconcile_session(pool, modal.App("test"), image, "session_test"))
    assert result == "started"
    exported = spans(capfire)
    root = exported["modal_openai.session.reconcile"]
    assert root.attributes["modal_openai.outcome"] == "started"
    assert root.attributes["gen_ai.conversation.id"] == "session_test"
    assert (
        exported["modal_openai.sandbox.create"].attributes["modal_openai.sandbox.id"] == "sb_test"
    )
    http = next(item for item in exported.values() if item.kind == SpanKind.CLIENT)
    assert http.context.trace_id == root.context.trace_id
    assert http.parent.span_id == exported["modal_openai.session.retrieve"].context.span_id
    assert "traceparent" in captured_headers[0]
    assert "api-key-sentinel" not in repr(exported)
    assert "response-body-sentinel" not in repr(exported)


def test_modal_worker_links_to_delivery_and_receives_export_settings(
    tmp_path,
    capfire,
    monkeypatch,
    verifier,
):
    functions = {}
    definitions = {}

    class App:
        def function(self, **options):
            def decorate(function):
                functions[function.__name__] = function
                definitions[function.__name__] = options
                function.spawn = SimpleNamespace(aio=AsyncMock())
                return function

            return decorate

    app = App()
    monkeypatch.setattr(server.modal, "App", lambda name: app)
    monkeypatch.setattr(server.modal, "fastapi_endpoint", lambda **kwargs: lambda f: f)
    secret = Mock(return_value="telemetry-secret")
    monkeypatch.setattr(server.modal.Secret, "from_dict", secret)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=export-key")
    monkeypatch.setenv("traceparent", "startup-context-must-not-leak")
    executor = tmp_path / "executor.sh"
    executor.write_text("#!/bin/bash\n")
    pool = Pool(name="test", agent_id="agent_test", image=modal.Image.debian_slim())
    server.build_app(pool, executor)
    settings = secret.call_args.args[0]
    assert settings["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=export-key"
    assert "traceparent" not in settings
    assert all("telemetry-secret" in options["secrets"] for options in definitions.values())

    asyncio.run(
        functions["webhook"](
            request_for(
                {
                    "type": "agent.session.failed",
                    "data": {"id": "session_test"},
                }
            )
        )
    )
    session_id, carrier = functions["reconcile"].spawn.aio.call_args.args
    assert session_id == "session_test"
    assert "traceparent" in carrier
    monkeypatch.setattr(server, "reconcile_session", AsyncMock(return_value="started"))
    assert asyncio.run(functions["reconcile"](session_id, carrier)) == "started"
    exported = spans(capfire)
    job = exported["modal_openai.worker.reconcile"]
    enqueue = exported["modal_openai.webhook.enqueue"]
    assert job.parent is None
    assert job.context.trace_id != enqueue.context.trace_id
    assert job.links[0].context.span_id == enqueue.context.span_id
    assert job.attributes["modal_openai.outcome"] == "started"


def test_cli_traces_actual_operation_and_flushes_without_arguments(
    capfire,
    monkeypatch,
):
    update = Mock()
    monkeypatch.setattr(
        cli.modal.Secret, "from_name", Mock(return_value=SimpleNamespace(update=update))
    )
    flushed = []
    monkeypatch.setattr(telemetry, "flush_telemetry", lambda: flushed.extend(spans(capfire)))
    cli.webhook_secret("test", secret="cli-secret-sentinel")
    exported = spans(capfire)
    command = exported["modal_openai.cli.webhook_secret"]
    assert exported["modal_openai.secret.update"].parent.span_id == command.context.span_id
    assert "modal_openai.cli.webhook_secret" in flushed
    assert "cli-secret-sentinel" not in repr(exported)
