"""Exercise exported spans and failure isolation, without external services."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import Mock

import logfire
import pytest
from opentelemetry import trace

from modal_agents import telemetry


@pytest.fixture(autouse=True)
def fresh_telemetry(monkeypatch):
    monkeypatch.setattr(telemetry._state, "configured", False)
    monkeypatch.setattr(telemetry._state, "disabled", False)


def completed_spans(capfire):
    return [
        item
        for item in capfire.exporter.exported_spans
        if item.attributes.get("logfire.span_type") == "span"
    ]


def test_parenting_and_context_round_trip(capfire):
    carrier = {}
    with telemetry.span("modal_openai.test.parent"):
        telemetry.inject_trace_context(carrier)
    assert set(carrier) <= {"traceparent", "tracestate"}
    assert "traceparent" in carrier
    with telemetry.continue_trace(carrier), telemetry.span("modal_openai.test.child"):
        pass
    parent, child = completed_spans(capfire)
    assert child.parent.span_id == parent.context.span_id
    assert child.context.trace_id == parent.context.trace_id
    assert not trace.get_current_span().get_span_context().is_valid


def test_detached_jobs_are_separate_traces_linked_to_delivery(capfire):
    carrier = {}
    with telemetry.span("modal_openai.test.delivery"):
        telemetry.inject_trace_context(carrier)
        with telemetry.new_root_span(carrier, "modal_openai.test.job"):
            pass
    job, delivery = completed_spans(capfire)
    assert job.parent is None
    assert job.context.trace_id != delivery.context.trace_id
    assert job.links[0].context.span_id == delivery.context.span_id


@pytest.mark.parametrize("carrier", [{}, {"traceparent": "malformed"}])
def test_requests_without_valid_context_do_not_inherit_warm_container(capfire, carrier):
    with telemetry.span("modal_openai.test.container"):
        original = trace.get_current_span().get_span_context()
        for _ in range(2):
            with (
                telemetry.continue_trace(carrier),
                telemetry.span("modal_openai.test.request"),
            ):
                pass
        assert trace.get_current_span().get_span_context() == original
    first, second, container = completed_spans(capfire)
    assert first.parent is None and second.parent is None
    assert (
        len(
            {
                first.context.trace_id,
                second.context.trace_id,
                container.context.trace_id,
            }
        )
        == 3
    )


def test_preserves_application_logfire_configuration(capfire, monkeypatch):
    configure = Mock(side_effect=AssertionError("must preserve application provider"))
    monkeypatch.setattr(logfire, "configure", configure)
    with telemetry.span("modal_openai.test.application"):
        pass
    configure.assert_not_called()
    assert completed_spans(capfire)[0].name == "modal_openai.test.application"


def test_failure_keeps_credentials_and_exception_contents_out_of_spans(capfire):
    error = subprocess.CalledProcessError(1, ["modal", "secret", "credential-sentinel"])

    @telemetry.instrument("modal_openai.test.command")
    def command(api_key):
        raise error

    with pytest.raises(subprocess.CalledProcessError) as caught:
        command("argument-sentinel")
    assert caught.value is error
    current = completed_spans(capfire)[0]
    assert current.attributes["error.type"] == "CalledProcessError"
    assert current.attributes["modal_openai.outcome"] == "failure"
    assert current.status.status_code == trace.StatusCode.ERROR
    assert "credential-sentinel" not in repr(current.attributes)
    assert "argument-sentinel" not in repr(current.attributes)
    assert not current.events


@pytest.mark.parametrize("failing", [False, True])
def test_command_flushes_after_spans_close_on_success_and_error(capfire, monkeypatch, failing):
    names_at_flush = []
    monkeypatch.setattr(
        telemetry,
        "flush_telemetry",
        lambda: names_at_flush.extend(item.name for item in completed_spans(capfire)),
    )

    @telemetry.instrument("modal_openai.test.command", flush=True)
    def command():
        with telemetry.span("modal_openai.test.child"):
            if failing:
                raise ValueError("failure")
        return 42

    if failing:
        with pytest.raises(ValueError):
            command()
    else:
        assert command() == 42
    assert names_at_flush == ["modal_openai.test.child", "modal_openai.test.command"]


def test_span_and_export_failures_do_not_change_application_result(monkeypatch):
    monkeypatch.setattr(telemetry._state, "configured", True)
    monkeypatch.setattr(logfire, "span", Mock(side_effect=RuntimeError("unavailable")))
    monkeypatch.setattr(logfire, "force_flush", Mock(side_effect=RuntimeError("offline")))
    with telemetry.span("modal_openai.test.unavailable") as current:
        assert current is None
    assert telemetry.flush_telemetry() is False
    with (
        pytest.raises(ValueError, match="application"),
        telemetry.span("modal_openai.test.unavailable"),
    ):
        raise ValueError("application")


def test_span_cleanup_failure_preserves_application_error(monkeypatch):
    monkeypatch.setattr(telemetry._state, "configured", True)
    manager = Mock()
    manager.__enter__ = Mock(return_value=Mock())
    manager.__exit__ = Mock(side_effect=RuntimeError("export failed"))
    monkeypatch.setattr(logfire, "span", Mock(return_value=manager))
    with (
        pytest.raises(ValueError, match="application"),
        telemetry.span("modal_openai.test.failure"),
    ):
        raise ValueError("application")
    manager.__exit__.assert_called_once_with(None, None, None)


def test_remote_settings_do_not_copy_unrelated_secrets_or_startup_context(monkeypatch):
    values = {
        "OTEL_SERVICE_NAME": "custom-service",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.example",
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=opaque",
        "OPENAI_API_KEY": "private",
        "traceparent": "startup",
        "LOGFIRE_TOKEN": "private",
    }
    for key in telemetry._REMOTE_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert telemetry.telemetry_environment() == {
        key: value for key, value in values.items() if key.startswith("OTEL_")
    }


def run_isolated(source, **settings):
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("OTEL_", "LOGFIRE_"))
    }
    environment.update(settings)
    return subprocess.run(
        [sys.executable, "-c", source],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )


def test_default_configuration_is_quiet_and_does_not_export():
    result = run_isolated("""
from unittest.mock import Mock
import requests
requests.Session.post = Mock(side_effect=AssertionError("unexpected export"))
from modal_agents.telemetry import span, flush_telemetry
with span("modal_openai.test.quiet"):
    pass
flush_telemetry()
requests.Session.post.assert_not_called()
""")
    assert not result.stdout and not result.stderr


def test_configuration_failure_is_noncritical():
    run_isolated("""
from unittest.mock import Mock
import logfire
logfire.configure = Mock(side_effect=ValueError("invalid configuration"))
from modal_agents.telemetry import span, flush_telemetry
with span("modal_openai.test.configuration") as current:
    assert current is None
assert not flush_telemetry()
logfire.configure.assert_called_once()
""")


def test_otlp_export_uses_configured_endpoint_headers_and_service():
    run_isolated(
        """
from unittest.mock import Mock
import requests
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as proto
calls = []
def post(self, url, **kwargs):
    assert self.headers["Authorization"] == "opaque"
    calls.append((url, kwargs))
    return Mock(ok=True)
requests.Session.post = post
from modal_agents.telemetry import span, flush_telemetry
with span("modal_openai.test.export"):
    pass
assert flush_telemetry()
assert calls
assert all(url == "https://collector.example/v1/traces" for url, _ in calls)
batch = proto.ExportTraceServiceRequest.FromString(calls[-1][1]["data"])
resource = batch.resource_spans[0]
attributes = {
    item.key: item.value.string_value for item in resource.resource.attributes
}
assert attributes["service.name"] == "custom-service"
assert any(span.name == "modal_openai.test.export"
           for scope in resource.scope_spans for span in scope.spans)
""",
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://collector.example/v1/traces",
        OTEL_SERVICE_NAME="custom-service",
        OTEL_EXPORTER_OTLP_HEADERS="Authorization=opaque",
    )
