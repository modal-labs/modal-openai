# Modal OpenAI Agents CLI — Usage Guide

A CLI tool to deploy OpenAI Agents API with Modal sandboxes using webhook-managed lifecycle.

## Installation

```bash
cd ~/Documents/GitHub/modal-openai
uv pip install -e .
```

Verify:
```bash
modal-agents --help
```

## Commands

### 1. Initialize a New Pool

```bash
modal-agents init research-agent
```

**Interactive wizard:**
1. Enter OpenAI API key (for managing agents)
2. Enter OpenAI executor key (restricted, for sandboxes only)
3. Select existing agent or create new one
4. Handler and pool files generated
5. Option to deploy immediately

**What it creates:**

```
agents/
  └── research-agent.py          # Pool configuration
hooks/
  └── research-agent_handler.py  # Webhook handler
  └── research-agent_executor.sh # Startup script
```

**`agents/research-agent.py` example:**

```python
"""Generated OpenAI Agents API configuration."""

import modal

OPENAI_SECRET_NAME = "openai-agents-research-agent"
OPENAI_AGENT_ID = "agent_abc123..."
WORKER_SECRET_NAMES = ()

pool = {
    "name": "research-agent",
    "agent_id": OPENAI_AGENT_ID,
    "image": modal.Image.debian_slim(python_version="3.14")
        .apt_install("git", "nodejs", "npm", "ripgrep")
        .run_commands("npm install -g @openai/codex@alpha"),
    # Uncomment for GPU:
    # "gpu": "A10G",
    # "cpu": 4,
    # "memory": 16384,
}
```

Edit this file to:
- Add system packages via `.apt_install()`
- Add Python packages via `.pip_install()`
- Add GPU/CPU resources
- Mount additional files/secrets

### 2. Deploy the Webhook Handler

```bash
modal-agents deploy research-agent
```

Or deploy all pools:
```bash
modal-agents deploy
```

This deploys the webhook handler to Modal. The handler:
- Listens for OpenAI webhook events
- Starts a Modal Sandbox when `agent.session.action_required` arrives
- Cleans up when `agent.session.failed` arrives

### 3. Register Webhook in OpenAI

1. Go to [OpenAI Project Settings → Webhooks](https://platform.openai.com/settings/project/webhooks)
2. Click "Create Webhook"
3. URL: Your deployed Modal webhook endpoint (shown after `modal-agents deploy`)
4. Events: `agent.session.action_required`, `agent.session.failed`
5. Save and copy the signing secret

4. Add secret to Modal:
```bash
modal secret update openai-agents-research-agent OPENAI_WEBHOOK_SECRET="sk_live_..."
```

### 4. Use in Your Application

```python
from openai import OpenAI

client = OpenAI(api_key="sk-...")

# Create agent session
session = await client.beta.agents.sessions.create(
    agent_id="agent_...",
    environment={
        "type": "self_hosted",
        "workspace_directory": "/workspace",
    },
)

# OpenAI will emit agent.session.action_required webhook
# Your handler will start a Modal sandbox
# The executor inside will connect back to OpenAI
# Agent will run and use the sandbox for code execution

# Stream events
async for event in session.stream(
    input="Analyze this data and write a report"
):
    if event.output_text_delta:
        print(event.output_text_delta, end="", flush=True)
```

### 5. List Agents and Pools

```bash
modal-agents list --api-key sk-...
```

Shows:
- Available OpenAI agents
- Configured pools in `agents/` directory

### 6. Remove a Pool

```bash
modal-agents destroy research-agent
```

Cleans up:
- Modal secret
- Pool configuration file
- Webhook handler
- Executor wrapper script

## Architecture Flow

```
┌────────────────┐
│  Your App      │
│                │
│  from openai   │
│  import OpenAI │
│                │
│  client.beta   │
│  .agents       │
│  .sessions     │
│  .create(...)  │
└────────┬───────┘
         │
         │ Creates session
         │
         ↓
┌─────────────────────────────────────┐
│  OpenAI Agents API                  │
│  (Cloud-hosted agent harness)       │
│  - Manages session state             │
│  - Runs agent reasoning              │
│  - Emits webhooks                    │
└────────┬────────────────────────────┘
         │
         │ agent.session.action_required
         │ (webhook POST)
         │
         ↓
┌──────────────────────────────────────┐
│  Modal Webhook Handler               │
│  (your hook/research-agent_handler.py)
│                                       │
│  On webhook:                         │
│  1. Verify signature                 │
│  2. Get environment_id               │
│  3. Start Modal Sandbox              │
└────────┬─────────────────────────────┘
         │
         ↓
┌──────────────────────────────────────┐
│  Modal Sandbox                       │
│  (compute, ephemeral)                │
│                                       │
│  Runs:                               │
│  codex exec-server                   │
│    --environment-id $ID              │
│    --remote https://api.openai.com   │
└────────┬─────────────────────────────┘
         │
         │ Outbound WebSocket
         │ to codex-cloud-environments.chatgpt.com
         │
         ↓
┌──────────────────────────────────────┐
│  OpenAI Agent                        │
│  - Receives executor connection      │
│  - Can now:                          │
│    • Run bash commands               │
│    • Access files in /workspace      │
│    • Call MCP tools                  │
│    • Create artifacts                │
└──────────────────────────────────────┘
```

## Environment Variables

**In `agents/research-agent.py` pool config:**

```python
OPENAI_SECRET_NAME = "openai-agents-research-agent"
OPENAI_AGENT_ID = "agent_..."
WORKER_SECRET_NAMES = ("my-db-secret",)
```

**In Modal Secret `openai-agents-research-agent`:**

```
OPENAI_API_KEY=sk-...
OPENAI_EXECUTOR_API_KEY=sk-...
OPENAI_WEBHOOK_SECRET=sk_live_...
```

**Passed to executor in sandbox:**

- `CODEX_API_KEY` — from `OPENAI_EXECUTOR_API_KEY`
- Any secrets in `WORKER_SECRET_NAMES`

## API Key Security

### OpenAI API Key
- **Scope**: Full permissions
- **Where**: Your app only (webhook handler)
- **Ability**: Create agents, manage sessions, run inference
- **Compromise**: Attacker can use your OpenAI account

### Executor Key
- **Scope**: Restricted to `List models → Read` only
- **Where**: Passed to sandboxes only (via env var)
- **Ability**: Only list available models
- **Compromise**: Limited — code running in agent can't:
  - Create agents or sessions
  - Run inference
  - Access other resources
  - Steal your main API key

**Why split them?** If agent-generated code reads the environment and exfiltrates `OPENAI_EXECUTOR_API_KEY`, damage is minimal. With one key, the attacker gets full account access.

**Setup:**

1. Create main key: [OpenAI API Keys](https://platform.openai.com/api-keys)
   - Full permissions
   - Keep in app, never in sandbox

2. Create executor key: Same location
   - **Grant only**: `List models → Read`
   - **Set**: Every other permission to `None`
   - Passed to sandbox via Modal Secret

3. Rotate periodically:
   ```bash
   modal secret update openai-agents-research-agent OPENAI_EXECUTOR_API_KEY="<new-key>"
   ```

## Customizing the Sandbox

Edit `agents/my-pool.py`:

```python
pool = {
    "name": "my-agent",
    "agent_id": "agent_...",
    # Start with base image
    "image": modal.Image.debian_slim(python_version="3.14")
        # Add system packages
        .apt_install("postgresql-client", "graphviz", "ffmpeg")
        # Add Python packages
        .pip_install("pandas", "numpy", "scikit-learn")
        # Run setup commands
        .run_commands("git clone https://github.com/..."),
    # Resource allocation
    "gpu": "A10G",      # GPU type
    "cpu": 4,           # CPU cores
    "memory": 16384,    # Memory in MB
}
```

## Testing

### Test Locally (No Deployment)

```bash
# Initialize without deploying
modal-agents init test-agent --no-deploy

# Review generated files
cat agents/test-agent.py
cat hooks/test-agent_handler.py
```

### Test with Manual Session

```python
import asyncio
from openai import OpenAI

client = OpenAI(api_key="sk-...")

session = client.beta.agents.sessions.create(
    agent_id="agent_...",
    environment={
        "type": "self_hosted",
        "workspace_directory": "/workspace",
    }
)

print(f"Session ID: {session.id}")
print(f"Environment ID: {session.environment.id}")

# Your webhook handler should automatically:
# 1. Receive action_required webhook
# 2. Start Modal Sandbox
# 3. Connect executor
```

Watch [Modal Dashboard](https://modal.com/apps) → Your app → Logs for sandbox creation.

## Troubleshooting

### "handler not deployed"
```bash
modal-agents deploy my-agent
```

### "secret not found"
Check Modal Secret exists:
```bash
modal secret list
```

Or create it:
```bash
modal secret create openai-agents-my-agent \
  OPENAI_API_KEY="sk-..." \
  OPENAI_EXECUTOR_API_KEY="sk-..." \
  OPENAI_WEBHOOK_SECRET="sk_live_..."
```

### "executor didn't connect"
Check:
1. Sandbox logs in Modal dashboard
2. `codex exec-server` running: `ps aux | grep codex`
3. Network access to `codex-cloud-environments.chatgpt.com`

### "webhook signature invalid"
Make sure `OPENAI_WEBHOOK_SECRET` matches OpenAI project settings.

## File Structure

```
.
├── modal_agents/
│   ├── __init__.py
│   ├── __main__.py
│   └── cli.py              # Main CLI logic
├── agents/                 # Generated pool configs
│   ├── research-agent.py
│   └── data-pipeline.py
├── hooks/                  # Generated webhook handlers
│   ├── research-agent_handler.py
│   └── data-pipeline_handler.py
├── pyproject.toml
├── README.md
└── USAGE.md
```

## Examples

### Example 1: Data Analysis Agent

```bash
modal-agents init analyst
```

Edit `agents/analyst.py`:
```python
pool = {
    "name": "analyst",
    "image": modal.Image.debian_slim()
        .pip_install("pandas", "matplotlib", "scipy"),
    "cpu": 4,
    "memory": 8192,
}
```

Deploy:
```bash
modal-agents deploy analyst
```

Use:
```python
session = client.beta.agents.sessions.create(
    agent_id="agent_...",
    environment={"type": "self_hosted", "workspace_directory": "/workspace"}
)

async for event in session.stream(
    input="Analyze the CSV in /workspace/data.csv and create plots"
):
    print(event.output_text_delta or "", end="", flush=True)
```

### Example 2: GPU-Enabled Agent

```bash
modal-agents init ml-agent
```

Edit `agents/ml-agent.py`:
```python
pool = {
    "name": "ml-agent",
    "image": modal.Image.debian_slim()
        .pip_install("torch", "transformers"),
    "gpu": "H100",
    "cpu": 8,
    "memory": 64000,
}
```

The agent can now train models, run inference on GPU, etc.

## Next Steps

1. **Deploy**: `modal-agents deploy my-pool`
2. **Register webhook**: Add to OpenAI project settings
3. **Create session**: Use `client.beta.agents.sessions.create()`
4. **Stream events**: `async for event in session.stream(...)`
5. **Monitor**: Watch Modal dashboard for sandbox activity

For more on OpenAI Agents API, see [official docs](https://platform.openai.com/docs/agents).
