"""Quiet, best-effort lifecycle tracing, following modal-cursor's OTEL conventions.

Only pass explicit operational metadata to spans. Never capture function arguments,
webhook bodies, credentials, or exception messages (subprocess errors contain argv).
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Callable, Generator, Mapping, MutableMapping
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from threading import Lock
from typing import Any, ParamSpec, TypeVar

import logfire
from opentelemetry import context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_P = ParamSpec("_P")
_R = TypeVar("_R")
_configured = False
_disabled = False
_lock = Lock()
_propagator = TraceContextTextMapPropagator()

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
    global _configured, _disabled
    with _lock:
        if _configured or _disabled:
            return
        try:
            # Same provider check as modal-cursor; also works with capfire's provider.
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
            _configured = True
        except Exception:
            # Observability must not prevent provisioning or webhook processing.
            _disabled = True


def set_attribute(current: Any, name: str, value: object) -> None:
    if current is not None:
        try:
            current.set_attribute(name, value)
        except Exception:
            pass


@contextmanager
def span(name: str, /, **attributes: object) -> Generator[Any, None, None]:
    """Use stable names and report failures without exporting exception contents."""
    configure_telemetry()
    manager = None
    current = None
    if not _disabled:
        try:
            manager = logfire.span(name, _span_name=name, **attributes)
            current = manager.__enter__()
        except Exception:
            manager = None
    try:
        yield current
    except BaseException as error:
        set_attribute(current, "error.type", type(error).__name__)
        set_attribute(current, "modal_openai.outcome", "failure")
        if current is not None:
            try:
                current.set_level("error")
            except Exception:
                pass
        raise
    finally:
        if manager is not None:
            try:
                # Do not let Logfire record exception messages/stack locals.
                manager.__exit__(None, None, None)
            except Exception:
                pass


def inject_trace_context(carrier: MutableMapping[str, str]) -> None:
    """Carry W3C traceparent/tracestate across a bounded remote operation."""
    try:
        _propagator.inject(carrier)
    except Exception:
        pass


@contextmanager
def continue_trace(carrier: Mapping[str, str]) -> Generator[None, None, None]:
    """Scope each request to its own incoming context and restore it afterward."""
    try:
        parent = _propagator.extract(carrier, context=context.Context())
        token = context.attach(parent)
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            context.detach(token)
        except Exception:
            pass


def flush_telemetry(timeout_millis: int = 2_000) -> bool:
    """Bound export latency at CLI/Modal invocation exit, including failure paths."""
    if not _configured or _disabled:
        return False
    try:
        return bool(logfire.force_flush(timeout_millis=timeout_millis))
    except Exception:
        return False


def instrument(name: str, *, flush: bool = False) -> Callable:
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
