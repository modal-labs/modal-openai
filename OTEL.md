# OpenTelemetry

Lifecycle tracing uses stable operation names, explicit operational attributes,
W3C context propagation, and independent traces for detached jobs linked to their
triggering request.

Tracing uses Logfire's OpenTelemetry SDK. Defaults are quiet: no console spans,
no Logfire account or token required, and no export without an OTLP endpoint.
An application that calls `logfire.configure(...)` first keeps its configuration.

## Export traces

Set these variables in the shell running the CLI:

```bash
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://collector.example/v1/traces"
export OTEL_SERVICE_NAME="modal-openai"
modal-agents list
```

Alternatively, set `OTEL_EXPORTER_OTLP_ENDPOINT` to the collector's base URL;
Logfire appends the signal paths. Export uses HTTP/protobuf. The trace-specific
endpoint is useful for collectors that accept only traces, such as Jaeger.
See [Logfire's OTLP configuration documentation](https://pydantic.dev/docs/logfire/guides/alternative-backends/).

Optional settings include `OTEL_EXPORTER_OTLP_HEADERS` (for collector
authentication), the corresponding `OTEL_EXPORTER_OTLP_TRACES_HEADERS`,
`OTEL_RESOURCE_ATTRIBUTES`, and general or trace-specific OTLP timeouts.
The default service name is `modal-openai`.

## Export from Modal

Deploy with the same variables set:

```bash
modal-agents deploy my-agent
```

The generated app passes supported telemetry settings to both the webhook and
reconciliation worker through a Modal Secret. Collector URLs and authentication
headers are not baked into image layers. Redeploy to change these settings.
Use an endpoint reachable from Modal; `localhost` in Modal refers to that
container, not your development machine.

The allowlist is defined in `modal_agents.telemetry.telemetry_environment`.
It includes service/resource metadata, OTLP endpoints, headers and timeouts,
and trace/metric exporter selection. It excludes unrelated environment variables,
application/executor keys, `LOGFIRE_TOKEN`, and deployment trace context.

## Trace boundaries and contents

| Operation | Spans |
| --- | --- |
| CLI commands | `modal_openai.cli.*`, with configuration, secret and deployment steps |
| Webhook delivery | `modal_openai.webhook.receive`, with verification, parsing and enqueue steps |
| Queued reconciliation | `modal_openai.worker.reconcile`, with session retrieval and sandbox operations |
| OpenAI session request | Scoped HTTPX client spans, including W3C propagation |
| Sandbox lifecycle | `modal_openai.sandbox.find`, `.create`, `.terminate`, `.detach` |

Each webhook continues its incoming `traceparent`/`tracestate`, or starts a fresh
trace when that context is absent or invalid. Request context is restored on exit,
so warm containers cannot merge unrelated deliveries. Arbitrary baggage is not
copied across the queue boundary.

Each queued reconciliation starts a separate trace linked to its enqueue span.
Retries also get independent traces linked to the same trigger. The OpenAI session ID is recorded as `gen_ai.conversation.id`, which Logfire
recognizes as a safe correlation field. Session and pool IDs allow correlation; IDs and payloads never appear in lifecycle span names.
Outcome attributes distinguish results such as `queued`, `started`,
`already_running`, `ignored_agent`, and `terminated`.

Attributes are limited to operational metadata, including pool/session/sandbox
IDs, HTTP method/status, outcomes, and error types. CLI arguments, API keys,
webhook bodies, and HTTP request/response bodies or headers are not captured.
Lifecycle exceptions record their type and error status without recording their
message or stack locals, which can contain subprocess arguments or payload data.

CLI commands and Modal invocations flush after their spans close, including
failure paths, with a 2-second flush timeout. Async handlers offload flushing to a
thread. Configuration/export failures are best-effort and do not replace the
application's result or exception.

Sandbox lifecycle tracing covers provisioning, lookup, termination, and detachment.
The Codex executor and OpenAI's managed agent harness provide their own telemetry.

## Verification

```bash
uv run pytest tests/test_telemetry.py tests/test_telemetry_integrations.py
```

These tests verify exported spans and protobuf OTLP payloads with in-memory
exporters and mocked HTTP transport. They cover propagation, job links, privacy,
failure handling, flushing, deployment settings, and actual lifecycle call sites
without deploying cloud resources or sending telemetry to an external collector.
