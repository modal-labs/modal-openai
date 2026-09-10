# modal-openai

Run OpenAI Agents in [Modal Sandboxes](https://modal.com/docs/guide/sandboxes).
OpenAI runs the agent; Modal provides an isolated environment where it can run
commands and edit files.

## Getting started

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), a Modal account,
and an OpenAI project with Agents API access. Your GitHub account must have
access to the [Agents API SDK](https://github.com/OpenAI-Early-Access/agents-api-python-preview).

```bash
git clone https://github.com/modal-labs/modal-openai.git
cd modal-openai
uv sync --locked
uv run modal setup
uv run modal-agents init research-agent
```

Setup guides you through selecting or creating an agent, configuring credentials,
and deploying its sandbox configuration. Have your OpenAI application key and a
separate restricted executor key ready. See [credentials](USAGE.md#credentials)
for the required permissions.

When prompted, register the supplied URL in
[OpenAI Project Webhooks](https://platform.openai.com/settings/project/webhooks)
for `agent.session.action_required` and `agent.session.failed`, then paste the
signing secret back into setup.

## Get a response

Ask the agent to run a command and tell you a joke:

```bash
uv run modal-agents smoke research-agent \
  --prompt "Use the shell to print hello, then tell me a joke."
```

The command prints the agent's response and cleans up the test session and
sandbox. It uses your OpenAI application key and incurs normal OpenAI and Modal
usage charges.

For sessions in your own application, see [examples/session.py](examples/session.py).
Use the agent ID saved in `agents/research-agent.py` and the same workspace path
(default `/workspace`).

## Configure the sandbox

Edit `agents/research-agent.py` to choose the image, CPU, memory, GPU, and timeout.
For example, add `.pip_install("pandas", "matplotlib")` to the image for data
analysis, or set `gpu="A10G"` for GPU workloads.

Deploy your changes:

```bash
uv run modal-agents deploy research-agent
```

New sandboxes use the updated configuration. Files remain available for the
sandbox's lifetime, which defaults to 30 minutes.

## More

- [CLI commands, credentials, and session management](USAGE.md)
- [Logging and tracing](OTEL.md)
- [Development and testing](CONTRIBUTING.md)
