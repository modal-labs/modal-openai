# modal-openai

Run **OpenAI Agents API preview** sessions in [Modal Sandboxes](https://modal.com/docs/guide/sandboxes).
OpenAI runs the agent harness; a signed webhook queues a Modal worker that starts
an isolated executor for the session.

You need an OpenAI project with Agents API preview access, Python 3.12+, and a
Modal account. Setup can select an existing agent or create one.

## Installation

The source repository is public. PyPI publishing is being prepared; use the
checkout instructions below until the first release is available.

The Agents API preview SDK requires separate GitHub access. In a checkout,
`uv sync --locked` installs it through the `preview` dependency group. A wheel
installation supports deployment and inspection without that SDK; agent
selection/creation and smoke tests require installing it separately:

```bash
python -m pip install "agent-api-sdk @ git+https://github.com/OpenAI-Early-Access/agents-api-python-preview.git@076c5f3fb9dbad9a1096a77943163fbc77061a0d"
```

See [RELEASING.md](RELEASING.md) for publishing setup.

## Getting started

```bash
uv sync --locked
uv run modal setup
uv run modal-agents init research-agent
```

The guided wizard explains credentials, selects/creates an agent, creates the
pool, and offers deployment. It then prints the exact webhook registration URL
and event types, prompts privately for the signing secret, redeploys, verifies
the running endpoint, and offers a live smoke test.

**Application and executor keys are both OpenAI Platform API keys.** The executor
key is a second restricted key used inside the sandbox, not a special key type.
The webhook signing secret (`whsec_...`) is separate and comes from registering
the endpoint. Application/session writes and controller/session reads are different
roles: supply `--controller-key` to separate them, or setup will store the
application key in the controller secret as well. Different key values do not
prove restricted scopes. See [Credentials](USAGE.md#credentials).

To review the generated files before deployment:

```bash
uv run modal-agents init research-agent --no-deploy
uv run modal-agents doctor research-agent --local
uv run modal-agents deploy research-agent
uv run modal-agents webhook-secret research-agent
uv run modal-agents smoke research-agent
```

Register the printed endpoint in [OpenAI Project Webhooks](https://platform.openai.com/settings/project/webhooks),
in the same project as the agent and application key, for
`agent.session.action_required` and `agent.session.failed`. The `webhook-secret`
command saves the secret, redeploys, and verifies readiness. A blank signing-secret
prompt in the wizard leaves setup pending and prints this resume command.

Generated files are `agents/POOL.py` (validated `Pool` configuration),
`agents/POOL_executor.sh`, and `hooks/POOL_handler.py`. Credentials live in three
separate Modal secrets. Use `--yes --agent-id agent_YOUR_ID` for unattended setup,
or `--yes --create-agent-name NAME --model MODEL` to explicitly create an agent.
Supply keys through the environment; see [USAGE.md](USAGE.md) for all options.

`doctor` checks metadata; `doctor --live` checks the public endpoint using a
signed challenge and unsigned rejection. `smoke` verifies a real OpenAI webhook,
a running sandbox, a completed turn with nonempty text, and cleanup, then prints
the response. It uses API/Modal credits. If your application key lives in a Modal
secret, use `smoke POOL --api-key-secret NAME` to run the client there.

Sessions must use the pool's exact saved `agent_id` and workspace (default
`/workspace`). An inline agent will not match the pool. Until the signing secret
is configured, the current endpoint returns HTTP 503; afterward unsigned requests
return HTTP 400. A deployment message alone does not prove readiness.

## How it works

1. The endpoint verifies the original webhook body and queues a reconciliation call.
2. One worker per pool retrieves current session state from OpenAI, filtering by agent ID.
3. Required environment connections start a sandbox named after the session ID.
   Duplicate deliveries reuse the running sandbox; deployment races also reuse the winner.
4. The sandbox receives only its executor key and explicitly configured worker secrets.
5. Failed or deleted sessions are cleaned up when reconciled. Each sandbox also has a
   hard lifetime limit (30 minutes by default).

The webhook only acknowledges after Modal accepts the queued call. Worker failures
retry up to three times. This is an event-driven integration: exhausted retries
and missed deletion events need operator reconciliation. Filesystems are ephemeral;
there is no snapshot/restore or periodic sweeper. Use one pool per agent to avoid
provisioning duplicate executors across separately deployed apps.

## Observability

Lifecycle logs identify pools, sessions, and sandboxes. OpenTelemetry spans cover
CLI operations, webhook verification and queueing, API retrieval, and sandbox
operations. Export is quiet by default. Set an OTLP endpoint to enable it;
see [OTEL.md](OTEL.md) for configuration, correlation, and privacy details.

## Development

```bash
uv sync --locked --all-groups
uv run ruff format --check modal_agents tests agents hooks
uv run ruff check modal_agents tests agents hooks
uv run mypy
uv run basedpyright
uv run coverage run -m pytest
uv run coverage report
uv build
```

Public CI checks formatting, lint, at least 80% branch-aware coverage, wheel
installation, and tests on Python 3.12 and 3.14. It skips the three SDK-dependent
test modules; run the full suite and both type checkers locally with preview access. Tests cover signatures, guided setup, readiness, smoke failure/cleanup paths, and mocked
cloud services. Run the [live smoke checklist](USAGE.md#live-smoke-check) to verify
deployment and executor connectivity before production or after executor upgrades.
