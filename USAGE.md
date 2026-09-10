# Modal OpenAI Agents CLI

Run OpenAI Agents API preview sessions in Modal sandboxes. OpenAI runs the agent;
this integration receives signed webhooks and starts its executor in a sandbox.
You need Agents API preview access, a Modal account, and Python 3.12 or later.

## Install

```bash
uv sync --locked --all-groups
uv run modal setup
uv run modal-agents --help
```

The preview SDK is pinned in package metadata and `uv.lock` to the revision used
by this integration.
Access to its GitHub repository may be required when installing dependencies.

## Credentials

Application and executor keys are both **OpenAI Platform API keys**. “Executor”
describes the key's role, not a separate product or key format. Create them in
[OpenAI API Keys](https://platform.openai.com/api-keys), in the project that owns
the agent and webhook.

| Credential | Used by | Required operations | Where it is stored |
| --- | --- | --- | --- |
| Application API key | Agent setup and your session client / smoke test | List or create agents as needed; create sessions, send input, read output, and delete test sessions | Local environment/private prompt, or an existing application secret you explicitly select |
| Controller API key | Modal reconciliation worker | Read current session state | `openai-agents-POOL-controller`, field `OPENAI_API_KEY` |
| Executor API key | Executor inside the sandbox | Connect the executor; configure a separate Restricted key with **List models → Read**, other permissions **None** | `openai-agents-POOL-executor`, field `CODEX_API_KEY` |
| Webhook signing secret | Webhook handler and readiness check | Verify webhook signatures; it grants no API access | `openai-agents-POOL-signing`, field `OPENAI_WEBHOOK_SECRET` |

The application needs write access for the operations above, not unrestricted
organization administration. The deployed controller only retrieves sessions.
Use `--controller-key` or `OPENAI_CONTROLLER_API_KEY` to give it a separate key
with session-read access. If omitted, `init` stores the application key in the
controller secret too, for convenience. The precise minimum Agents API preview
permission labels must be checked in your project; the wizard does not infer or
validate key scopes. It checks that executor and controller/application key
values differ. The successful E2E test demonstrates connectivity, not that the
executor key has the intended restrictions.

`OPENAI_EXECUTOR_API_KEY` is the local setup variable; the wizard maps it to
`CODEX_API_KEY` in Modal. The sandbox receives this key and explicitly configured
worker secrets. It does not receive the controller or signing secret.

A signing secret starts with `whsec_`. OpenAI gives it to you when you register
the webhook. It is not an API key and cannot replace either API credential.
Prompts hide credential input. Prefer prompts or environment variables to putting
secrets directly in command arguments or checked-in files.

## Guided setup

```bash
uv run modal-agents init research-agent
```

The wizard:

1. Explains the credentials, then prompts privately for application and executor keys.
2. Lists agents in the application key's project. Select one, enter an existing ID,
   or choose **new** and supply an agent name and model. Creating an agent requires
   application write access. A failed listing is reported rather than treated as an empty list.
3. Creates three separate Modal secrets and the pool, executor, and handler files.
4. Offers deployment. You can review/customize first with `--no-deploy`.
5. Prints the exact deployed URL, registration page, project requirement, and event names.
6. Prompts privately for the signing secret. A blank value leaves setup pending
   and prints the resume command. Otherwise, it saves the secret, redeploys, and
   verifies the public endpoint with a signed challenge and an unsigned request.
7. Offers a real smoke test, which creates billable API/Modal work and cleans up
   its API session and sandbox afterward.

The pool serves one saved agent ID. Your sessions must use that exact `agent_id`.
An inline `agent={...}` creates a different agent and will not match the pool.
Use one pool per agent to avoid duplicate provisioning across apps.

### Review before deploying

```bash
uv run modal-agents init research-agent --no-deploy
uv run modal-agents doctor research-agent --local
uv run modal-agents deploy research-agent
uv run modal-agents webhook-secret research-agent
```

Generated files:

```text
agents/research-agent.py           # Pool configuration
agents/research-agent_executor.sh  # Executor startup command
hooks/research-agent_handler.py    # Modal deployment entry point
```

### Unattended setup

Supply `OPENAI_API_KEY` and `OPENAI_EXECUTOR_API_KEY` through your environment.
Optionally supply `OPENAI_CONTROLLER_API_KEY`. Use either an existing agent:

```bash
uv run modal-agents init research-agent --agent-id agent_YOUR_ID --yes --no-deploy
```

Or explicitly create one:

```bash
uv run modal-agents init research-agent --create-agent-name 'Research agent' --model gpt-5.5 --yes --no-deploy
```

`--yes` does not guess a model or create an agent unless requested. Existing files
cause `init` to stop without overwriting them. Existing Modal secret values are
retained; rerunning setup is not credential rotation. Remove `--no-deploy` to
deploy immediately. If `OPENAI_WEBHOOK_SECRET` is also supplied, the wizard applies
it and verifies readiness. Otherwise, it prints the registration/resume steps.
An unattended setup does not automatically create a smoke-test session.

## Register the webhook

Open [OpenAI Project Webhooks](https://platform.openai.com/settings/project/webhooks)
in the same project as the application key and agent. Use the exact URL printed
by `deploy` or `webhook-secret`, and subscribe to:

- `agent.session.action_required`
- `agent.session.failed`

Copy the signing secret, then run:

```bash
uv run modal-agents webhook-secret research-agent
```

This command saves the secret, redeploys to apply it, and runs live readiness
checks. Use `--no-deploy` to save it for later; then run `deploy` and `doctor --live`
yourself. Before a real signing secret is configured, the current handler returns
HTTP 503. Once configured, an unsigned request must return HTTP 400.

Webhook registration remains a step in OpenAI Platform. Neither saving a secret
nor passing readiness proves that OpenAI is subscribed to the right events.

## Readiness and verification

| Command | What it proves |
| --- | --- |
| `doctor POOL --local` | Local pool, handler, and executor files are valid |
| `doctor POOL` | Local files, required Modal secret names, app, and endpoint metadata exist |
| `doctor POOL --live` | The public URL rejects unsigned requests and answers a fresh signed challenge using the current readiness protocol |
| `smoke POOL` | A real OpenAI session completes with nonempty text and a running webhook-created sandbox; session and sandbox cleanup completes |

Live readiness retries temporarily stale or unhealthy endpoints for up to roughly
one minute plus in-flight request time. HTTP 200 alone is not enough: an older
handler that merely ignores the probe cannot pass. Probe events never create
sessions or enqueue reconciliation. Readiness cannot verify API key permissions,
OpenAI webhook registration, or executor connectivity; use the smoke test.

## Live smoke check

Supply an application key with session write access as `OPENAI_API_KEY`, or enter
it at the private prompt:

```bash
uv run modal-agents smoke research-agent
uv run modal-agents smoke research-agent --prompt 'Run a shell command to print hello from the sandbox.' --timeout 300
```

The default prompt asks for a short joke. The command first checks readiness,
then creates a session for the pool's saved agent and workspace. It does not
provision a sandbox directly: OpenAI's registered webhook must cause that.
Success requires a completed turn, nonempty response text, a running named
sandbox, and successful cleanup. Timeout, failed/cancelled turns, empty output,
missing sandboxes, and cleanup errors fail the command. The timeout accepts
30–600 seconds. Session creation and cleanup also have their own API/Modal latency.

If the application key already lives in a Modal secret, run the client in a
temporary Modal function without downloading the key:

```bash
uv run modal-agents smoke research-agent --api-key-secret my-application-secret
```

That secret must contain `OPENAI_API_KEY` with session write access. Do not choose
a controller key restricted to reads. If the controller secret intentionally
holds the application key, it can also be used here. This mode packages the
locally installed preview SDK and needs no additional GitHub credential in Modal.

The test prints session/sandbox IDs and the actual response. The deployed endpoint
and saved agent remain available. On a cleanup error, use the printed session ID
and Modal logs to finish cleanup; do not treat the run as passed.

## Use in your application

The included example collects both typed deltas and final text snapshots. The
live API can deliver a final snapshot without any deltas:

```python
import asyncio
from agent_api_sdk import AgentAPISDK
from modal_agents.output import TextOutput

async def main():
    async with AgentAPISDK(timeout=300) as client:
        session = await client.sessions.create(
            agent_id="agent_YOUR_ID",  # Must match the pool
            environment={"type": "self_hosted", "workspace_directory": "/workspace"},
        )
        output = TextOutput()
        async for event in session.stream(input="Tell me a joke."):
            output.add(event)
        print(output.text)
        # Own the session lifecycle: deletion and sandbox cleanup are separate.

asyncio.run(main())
```

Use `smoke` for verified completion and automatic test cleanup. The simpler
`examples/session.py` example deletes its API session unless `--keep-session` is
set, but leaves sandbox termination to the caller. Do not rely on the preview
SDK's `event.output_text_delta` convenience property. `TextOutput` uses
`SessionTurnOutputTextDeltaEvent.delta` and `SessionTurnOutputTextDoneEvent.text`;
final snapshots replace partial text instead of duplicating it.

## Customize the pool

The generated configuration is a validated `Pool`, not a dictionary:

```python
import modal
from modal_agents.pool import Pool

pool = Pool(
    name="research-agent",
    agent_id="agent_YOUR_ID",
    image=(modal.Image.debian_slim()
        .apt_install("git", "nodejs", "npm", "ripgrep")
        .run_commands("npm install -g @openai/codex@alpha", "mkdir -p /workspace")),
    cpu=2,
    memory=4096,
    timeout=1800,
    workspace="/workspace",
    # gpu="A10G",
    # worker_secret_names=("my-data-secret",),
)
```

Add packages and resources here. Pin a tested executor version before production
use. Keep the session workspace equal to `pool.workspace`. The default sandbox
lifetime is 30 minutes; filesystems are ephemeral. Worker secret names must be
separate from the integration's `openai-agents-*` secrets.

## Rotation and removal

Update a specific credential through Modal's secret UI or SDK, preserving its
field name from the credentials table. Then redeploy the pool to refresh its
containers. New executor credentials apply to new sandboxes; existing sandboxes
retain their original environment. For signing-secret rotation use
`modal-agents webhook-secret POOL`, which applies and checks the change.

Older deployments may have a combined `openai-agents-POOL` secret. Current code
requires the three separate secrets listed above. Migrate values deliberately;
the wizard does not automatically extract or split legacy secrets.

```bash
uv run modal-agents list
uv run modal-agents deploy           # All local pools, sorted
uv run modal-agents destroy research-agent
```

`list` lists local pools; it takes no `--api-key` option. `destroy` stops the Modal
app and removes the three local pool files. It **retains Modal secrets**. Remove
the OpenAI webhook, end API sessions, and delete retained secrets separately when
appropriate. Noninteractive removal requires `--yes`.

## Troubleshooting

- **HTTP 503:** Configure the signing secret and redeploy.
- **HTTP 400:** Expected for unsigned requests. For genuine OpenAI deliveries,
  check the signing secret, registration project, and delivery details.
- **HTTP 500 / old handler:** Inspect `modal app history APP` and
  `modal app logs APP --timestamps --show-container-id`. A successful deployment
  does not prove that old containers have finished exiting. After reviewing
  active work, `modal app rollover APP --strategy recreate` replaces containers;
  it can interrupt work. Rerun `doctor POOL --live` afterward. Do not blindly retry
  a broken handler or treat a rollover timeout as a successful readiness check.
- **Readiness function missing:** Redeploy the current handler before using `--live`.
- **Smoke times out:** Check OpenAI delivery records, event subscriptions, exact
  URL/project/agent ID, executor logs, and API key access. An inline agent will be
  ignored. The smoke test has a bounded wait and attempts cleanup on failure.
- **Completed turn but empty text:** Handle both typed delta and done events,
  preferably with `TextOutput`. The API can send a final snapshot without deltas.
  A completed event by itself is not proof of a captured answer.
- **Serialization error:** The current worker resolves the Modal app at runtime.
  Do not capture a running `modal.App` in serialized function closures.

See [OTEL.md](OTEL.md) for tracing, correlation, and exporter configuration.
