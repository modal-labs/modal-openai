# Modal + OpenAI Agents API

Deploy OpenAI Agents API sessions with Modal sandboxes. The agent runs in OpenAI's managed harness, while code execution happens in your Modal sandbox.

## Quick Start

### Install

```bash
uv pip install -e .
```

### Initialize a Pool

```bash
modal-agents init research-agent
```

The wizard will:
1. Ask for your OpenAI API key
2. Ask for your restricted executor key  
3. List existing agents or create a new one
4. Generate pool configuration and webhook handler
5. Optionally deploy the webhook handler to Modal

### Generated Files

After `init`, you'll have:

- **`agents/research-agent.py`** — Pool configuration (CPU, GPU, dependencies)
- **`hooks/research-agent_handler.py`** — Webhook handler for sandbox lifecycle
- **`agents/research-agent_executor.sh`** — Executor startup script

### Deploy

```bash
modal-agents deploy research-agent
```

### Register the Webhook

1. Go to [OpenAI Project Settings → Webhooks](https://platform.openai.com/settings/project/webhooks)
2. Create webhook pointing to deployed handler
3. Subscribe to: `agent.session.action_required`, `agent.session.failed`
4. Save signing secret and add to Modal Secret

## How It Works

OpenAI sends webhook → Handler starts Modal sandbox → Executor connects back → Agent runs

## API Keys

- **`OPENAI_API_KEY`**: Full permissions, manage agents (app only)
- **`OPENAI_EXECUTOR_API_KEY`**: Restricted to `List models`, passed to sandbox only

## Commands

```bash
modal-agents init my-agent      # Create pool
modal-agents list               # Show agents
modal-agents deploy my-agent    # Deploy handler
modal-agents destroy my-agent   # Remove pool
```

## References

- [OpenAI Agents API Docs](https://platform.openai.com/docs/agents)
- [Modal Sandboxes](https://modal.com/docs/guide/sandboxes)
