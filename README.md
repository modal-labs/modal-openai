# modal-openai

Run **OpenAI Agents API preview** sessions in [Modal Sandboxes](https://modal.com/docs/guide/sandboxes).
OpenAI runs the agent harness; a signed webhook queues a Modal worker that starts
an isolated executor for the session.

You need an OpenAI project with Agents API access, an existing
agent ID, a separate restricted executor key, Python 3.12+, and a Modal account.

## Getting started

From this checkout, install the package and configure Modal:

```bash
uv sync --locked
uv run modal setup
uv run modal-agents init research-agent --agent-id agent_YOUR_ID --no-deploy
```

The wizard prompts privately for `OPENAI_API_KEY` and
`OPENAI_EXECUTOR_API_KEY`. For unattended setup, set those variables and
`OPENAI_AGENT_ID`, then use `--yes`. Application and executor keys must differ.
See [Credentials](USAGE.md#credentials) for executor key permissions and ownership.

Initialization creates three separate Modal secrets and three editable files:

- `agents/research-agent.py`: image, resources, timeout, agent ID, worker secrets.
- `agents/research-agent_executor.sh`: executor startup command.
- `hooks/research-agent_handler.py`: deployment entry point.

Review the pool and deploy it:

```bash
uv run modal-agents doctor research-agent --local
uv run modal-agents deploy research-agent
```

Register the printed endpoint in [OpenAI Project Webhooks](https://platform.openai.com/settings/project/webhooks)
for `agent.session.action_required` and `agent.session.failed`. Copy the signing
secret, then update and redeploy:

```bash
uv run modal-agents webhook-secret research-agent
uv run modal-agents deploy research-agent
uv run modal-agents doctor research-agent
```

Until the signing secret is configured, the endpoint returns HTTP 503.
Create a self-hosted session for the configured agent with
`workspace_directory="/workspace"`. See [USAGE.md](USAGE.md) for the full
walkthrough, customization, credential rotation, and troubleshooting.

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

CI enforces these checks, at least 80% branch-aware coverage, wheel installation,
and tests on Python 3.12 and 3.14. Tests verify webhook signatures and use mocked
cloud services. Run the [live smoke checklist](USAGE.md#live-smoke-check) to verify
deployment and executor connectivity before production or after executor upgrades.
