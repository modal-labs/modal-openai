# Modal OpenAI Agents CLI — Summary

A command-line tool to scaffold and deploy OpenAI Agents API sessions on Modal sandboxes.

## What Was Built

### Core Files

- **`modal_agents/cli.py`** — Main CLI implementation (480+ lines)
  - `init` command: Interactive wizard for pool setup
  - `deploy` command: Deploy webhook handlers to Modal
  - `list` command: Show agents and pools
  - `destroy` command: Clean up pools

- **`modal_agents/__main__.py`** — CLI entry point
- **`modal_agents/__init__.py`** — Package setup
- **`pyproject.toml`** — Dependencies and CLI script registration
- **`README.md`** — Quick start guide
- **`USAGE.md`** — Comprehensive usage guide (400+ lines)

### Installation

```bash
cd ~/Documents/GitHub/modal-openai
uv pip install -e .
```

## Commands

### 1. Initialize a Pool

```bash
modal-agents init research-agent
```

Generates:
- `agents/research-agent.py` — Pool configuration
- `hooks/research-agent_handler.py` — Webhook handler
- `agents/research-agent_executor.sh` — Executor startup

The wizard:
1. Asks for OpenAI API key
2. Asks for restricted executor key
3. Lists/creates agents
4. Generates boilerplate
5. Optionally deploys

### 2. Deploy Handler

```bash
modal-agents deploy research-agent
```

Deploys webhook handler to Modal.

### 3. List Resources

```bash
modal-agents list --api-key sk-...
```

Shows available agents and configured pools.

### 4. Destroy Pool

```bash
modal-agents destroy research-agent
```

Removes pool, handler, and secrets.

## Generated Files

### `agents/research-agent.py`

Pool configuration:
```python
pool = {
    "name": "research-agent",
    "agent_id": "agent_...",
    "image": modal.Image.debian_slim()
        .apt_install("git", "nodejs", "npm", "ripgrep")
        .run_commands("npm install -g @openai/codex@alpha"),
    # Add GPU, CPU, memory as needed
}
```

Edit to customize:
- Add system packages
- Add Python packages
- Allocate GPU/CPU
- Mount secrets

### `hooks/research-agent_handler.py`

Webhook handler (generated):
```python
@app.web_endpoint()
async def webhook_handler(request: dict) -> dict:
    """Receive OpenAI webhooks and manage sandbox lifecycle."""
    # Verify signature
    # On action_required: Start sandbox
    # On session_failed: Cleanup
```

Handles:
- `agent.session.action_required` → Start sandbox
- `agent.session.failed` → Cleanup
- Signature verification

## Integration Flow

```
Your App
  ↓
  client.beta.agents.sessions.create(...)
  ↓
OpenAI Agents API
  ↓
Webhook POST (action_required)
  ↓
Modal Handler
  ↓
Start Modal Sandbox
  ↓
Run Executor (codex exec-server)
  ↓
Connect back to OpenAI
  ↓
Agent runs with sandbox access
```

## Key Features

✅ **Wizard-based setup** — No manual configuration needed
✅ **Webhook-managed** — Automatic sandbox lifecycle
✅ **Secure** — Separate API keys for app vs sandbox
✅ **Customizable** — Edit generated pool configs
✅ **Multi-pool** — Deploy multiple agents simultaneously
✅ **Templated** — Generate boilerplate in seconds

## Security

Two API keys:
1. **App key** — Full permissions, app only
2. **Executor key** — Restricted (list models only), passed to sandbox

If executor key leaks: Limited damage (can't create agents, run inference, etc.)

## Files Generated

After `modal-agents init my-agent`:

```
agents/
  my-agent.py                  # Pool config (edit this)
hooks/
  my-agent_handler.py          # Webhook handler (deploy this)
agents/
  my-agent_executor.sh         # Startup script
```

## Next Steps

1. Install: `uv pip install -e .`
2. Initialize: `modal-agents init my-agent`
3. Review: Edit `agents/my-agent.py`
4. Deploy: `modal-agents deploy my-agent`
5. Register webhook in OpenAI project settings
6. Create sessions and stream events

See `USAGE.md` for detailed examples and troubleshooting.
