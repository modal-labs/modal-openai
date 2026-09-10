# modal-openai

Run OpenAI Agents in [Modal Sandboxes](https://modal.com/docs/guide/sandboxes).
OpenAI runs the agent; Modal provides an isolated environment where it can run
commands and edit files.

## Getting started

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), Git, a Modal account,
and an OpenAI project with Agents API access. Your GitHub account must have
access to the [Agents API SDK](https://github.com/OpenAI-Early-Access/agents-api-python-preview).

```bash
mkdir my-agent
cd my-agent
uv venv
source .venv/bin/activate
uv pip install modal-openai \
  "agent-api-sdk @ git+https://github.com/OpenAI-Early-Access/agents-api-python-preview.git@076c5f3fb9dbad9a1096a77943163fbc77061a0d"
modal setup
modal-agents init research-agent
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
modal-agents smoke research-agent \
  --prompt "Use the shell to print hello, then tell me a joke."
```

The command prints the agent's response and cleans up the test session and
sandbox. It uses your OpenAI application key and incurs normal OpenAI and Modal
usage charges. If the key is stored in a Modal Secret, add
`--api-key-secret YOUR_SECRET_NAME`.

For sessions in your own application, see [examples/session.py](examples/session.py).
Use the agent ID saved in `agents/research-agent.py` and the same workspace path
(default `/workspace`).

## Configure the sandbox

Edit `agents/research-agent.py` to choose the image, CPU, memory, GPU, and timeout.
For example, add `.pip_install("pandas", "matplotlib")` to the image for data
analysis, or set `gpu="A10G"` for GPU workloads.

Deploy your changes:

```bash
modal-agents deploy research-agent
```

New sandboxes use the updated configuration. Files remain available for the
sandbox's lifetime, which defaults to 30 minutes.

## More

- [CLI commands, credentials, and session management](USAGE.md)
- [Logging and tracing](OTEL.md)
- [Development and testing](CONTRIBUTING.md)

## License

[MIT](LICENSE).
