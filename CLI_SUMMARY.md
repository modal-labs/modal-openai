# Modal OpenAI Agents CLI — command summary

See [USAGE.md](USAGE.md) for the complete setup, credential mapping, and troubleshooting.

| Command | Behavior |
| --- | --- |
| `init POOL` | Explain credentials; choose/create an agent; create three secrets and files; offer deployment, webhook registration handoff, readiness, and a smoke test |
| `init POOL --no-deploy` | Generate configuration and secrets for review; print resume commands |
| `deploy [POOL]` | Validate and deploy one pool, or all local pools; deployment success is not a readiness result |
| `webhook-secret POOL` | Prompt privately, save the dedicated signing secret, redeploy, and check live readiness |
| `webhook-secret POOL --no-deploy` | Save the signing secret for a later deployment |
| `doctor [POOL] --local` | Check local configuration only |
| `doctor [POOL]` | Check configuration, secret names, and deployment metadata |
| `doctor [POOL] --live` | Also require an unsigned rejection and a correct signed challenge response |
| `smoke POOL` | Run a real webhook-driven session; require nonempty output and a running sandbox; clean up and print the response |
| `list` | List locally configured pools |
| `destroy POOL` | Stop the app and remove local configuration; retain Modal secrets |

## Quick start

```bash
uv sync --locked
uv run modal setup
uv run modal-agents init research-agent
```

The wizard lists saved agents or creates one with your chosen name and model.
For unattended setup, supply credentials through the environment and use
`--yes --agent-id agent_YOUR_ID`, or `--yes --create-agent-name NAME --model MODEL`.

Both application and executor credentials are OpenAI Platform API keys. The
executor key is a separate restricted key placed in the sandbox. Optionally use
`--controller-key` for the controller's session-read access; otherwise the
application key is stored in the controller secret too. The webhook signing
secret (`whsec_...`) is a different credential obtained during registration.
The wizard does not verify key scopes.

## Resume after review or webhook registration

```bash
uv run modal-agents deploy research-agent
uv run modal-agents webhook-secret research-agent
uv run modal-agents smoke research-agent
```

Register the printed endpoint in the same OpenAI project as the agent and
application key for `agent.session.action_required` and `agent.session.failed`.
Sessions must use the pool's exact saved agent ID and workspace.

## Generated configuration

- `agents/POOL.py`: validated `Pool` configuration, including a Modal image and resources.
- `agents/POOL_executor.sh`: `codex exec-server` startup command.
- `hooks/POOL_handler.py`: `build_app` deployment entry point.

Modal secrets are separate: `openai-agents-POOL-controller` (`OPENAI_API_KEY`),
`openai-agents-POOL-executor` (`CODEX_API_KEY`), and
`openai-agents-POOL-signing` (`OPENAI_WEBHOOK_SECRET`).

Readiness verifies the running endpoint. Only a real smoke test verifies the
OpenAI registration and executor path. The smoke test incurs API/Modal usage and
cleans up its session and sandbox; the deployed app and saved agent remain.
