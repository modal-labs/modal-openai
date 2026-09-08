"""Quiet, best-effort OpenTelemetry tracing for CLI, webhook, and sandbox lifecycles.

Only pass explicit operational metadata to spans. Never capture function arguments,
webhook bodies, credentials, or exception messages (subprocess errors contain argv).
"""

import os
import warnings
from collections.abc import (
    Awaitable,
    Callable,
    Generator,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from threading import Lock
from typing import ParamSpec, TypeVar

import httpx
import logfire
from logfire import LogfireSpan
from opentelemetry import context, trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import Attributes

_P = ParamSpec("_P")
_R = TypeVar("_R")


@dataclass
class _TelemetryState:
    configured: bool = False
    disabled: bool = False


_state = _TelemetryState()
_lock = Lock()
_propagator = TraceContextTextMapPropagator()
_TELEMETRY_ERRORS = (OSError, RuntimeError, ValueError)

# Passed through a Modal Secret, never baked into an image or span attributes.
# In particular, do not carry a deployment's traceparent into a warm container.
_REMOTE_ENV = (
    "OTEL_SERVICE_NAME",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TIMEOUT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",
    "OTEL_TRACES_EXPORTER",
    "OTEL_METRICS_EXPORTER",
)


def telemetry_environment() -> dict[str, str]:
    """Capture supported deployment settings without copying unrelated secrets."""
    return {key: os.environ[key] for key in _REMOTE_ENV if os.environ.get(key)}


def configure_telemetry() -> None:
    """Preserve explicit Logfire setup; otherwise use silent, OTLP-only defaults."""
    with _lock:
        if _state.configured or _state.disabled:
            return
        try:
            # Preserve an existing provider, including one configured by the test fixture.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                instance = getattr(logfire, "DEFAULT_LOGFIRE_INSTANCE", None)
                proxy = getattr(instance, "_tracer_provider", None)
                provider = getattr(proxy, "provider", None)
            if provider is None or provider.__class__.__name__ == "NoOpTracerProvider":
                logfire.configure(
                    send_to_logfire=False,
                    service_name=os.environ.get("OTEL_SERVICE_NAME", "modal-openai"),
                    console=False,
                    inspect_arguments=False,
                    distributed_tracing=True,
                )
            logfire.add_non_user_code_prefix(Path(__file__))
            _state.configured = True
        except _TELEMETRY_ERRORS:
            # Observability must not prevent provisioning or webhook processing.
            _state.disabled = True


def set_attribute(current: LogfireSpan | None, name: str, value: object) -> None:
    if current is not None:
        with suppress(*_TELEMETRY_ERRORS):
            current.set_attribute(name, value)


@contextmanager
def span(
    name: str,
    attributes: Mapping[str, object] | None = None,
    /,
    *,
    links: Sequence[tuple[trace.SpanContext, Attributes]] = (),
    kind: trace.SpanKind = trace.SpanKind.INTERNAL,
) -> Generator[LogfireSpan | None, None, None]:
    """Use stable names and report failures without exporting exception contents."""
    configure_telemetry()
    manager = None
    current = None
    if not _state.disabled:
        try:
            manager = logfire.span(name, _span_name=name, _links=links, _span_kind=kind)
            for key, value in (attributes or {}).items():
                set_attribute(manager, key, value)
            current = manager.__enter__()
        except _TELEMETRY_ERRORS:
            manager = None
    try:
        yield current
    except BaseException as error:
        set_attribute(current, "error.type", type(error).__name__)
        set_attribute(current, "modal_openai.outcome", "failure")
        if current is not None:
            with suppress(*_TELEMETRY_ERRORS):
                current.set_level("error")
        raise
    finally:
        if manager is not None:
            with suppress(*_TELEMETRY_ERRORS):
                # Do not let Logfire record exception messages/stack locals.
                manager.__exit__(None, None, None)


def inject_trace_context(carrier: MutableMapping[str, str]) -> None:
    """Carry W3C traceparent/tracestate across a bounded remote operation."""
    _propagator.inject(carrier)


@contextmanager
def continue_trace(carrier: Mapping[str, str]) -> Generator[None, None, None]:
    """Scope each request to its own incoming context and restore it afterward."""
    parent = _propagator.extract(carrier, context=context.Context())
    token = context.attach(parent)
    try:
        yield
    finally:
        context.detach(token)


@contextmanager
def new_root_span(
    carrier: Mapping[str, str], name: str, /, **attributes: object
) -> Generator[LogfireSpan | None, None, None]:
    """Give detached jobs independent traces linked to their triggering request."""
    links: Sequence[tuple[trace.SpanContext, Attributes]] = ()
    inherited = _propagator.extract(carrier, context=context.Context())
    parent = trace.get_current_span(inherited).get_span_context()
    if parent.is_valid:
        links = ((parent, {"modal_openai.link.type": "trigger"}),)
    with continue_trace({}), span(name, attributes, links=links) as current:
        yield current


def flush_telemetry(timeout_millis: int = 2_000) -> bool:
    """Bound export latency at CLI/Modal invocation exit, including failure paths."""
    if not _state.configured or _state.disabled:
        return False
    try:
        return bool(logfire.force_flush(timeout_millis=timeout_millis))
    except _TELEMETRY_ERRORS:
        return False


def instrument_httpx(client: httpx.Client | httpx.AsyncClient) -> None:
    """Instrument this API client only; bodies and headers stay uncaptured."""
    configure_telemetry()
    if not _state.disabled:
        with suppress(*_TELEMETRY_ERRORS):
            logfire.instrument_httpx(client, capture_all=False)


def instrument_async(
    name: str,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Keep a span open across the actual await, without recording arguments."""

    def decorator(function: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
        @wraps(function)
        async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with span(name):
                return await function(*args, **kwargs)

        return wrapped

    return decorator


def instrument(name: str, *, flush: bool = False) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Trace synchronous lifecycle operations without collecting their arguments."""

    def decorator(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(function)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                with span(name):
                    return function(*args, **kwargs)
            finally:
                if flush:
                    flush_telemetry()

        return wrapped

    return decorator
