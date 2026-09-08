"""CLI for deploying OpenAI Agents API on Modal sandboxes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from string import Template
from typing import Annotated

import cyclopts
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.text import Text

app = cyclopts.App(name="modal-agents")

AGENTS_DIR = Path("agents")
HOOKS_DIR = Path("hooks")
_PROMPT_SUFFIX = Text(" › ", style="dim")
_console = Console()


class _WizardPrompt(Prompt):
    prompt_suffix = _PROMPT_SUFFIX  # type: ignore[assignment]


class _WizardConfirm(Confirm):
    prompt_suffix = _PROMPT_SUFFIX  # type: ignore[assignment]


PoolNameArg = Annotated[
    str, cyclopts.Parameter(help="Agent pool name, for example research-agent.")
]
ApiKeyOption = Annotated[
    str, cyclopts.Parameter(help="OpenAI API key for agent creation/management.")
]
ExecutorKeyOption = Annotated[
    str, cyclopts.Parameter(help="Restricted OpenAI executor key for sandboxes.")
]
YesOption = Annotated[bool, cyclopts.Parameter(name=("--yes", "-y"), negative=False)]


WEBHOOK_HANDLER_TEMPLATE = '''"""OpenAI Agents API webhook handler for sandbox lifecycle management."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
import modal
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_EXECUTOR_API_KEY = os.environ.get("OPENAI_EXECUTOR_API_KEY")
OPENAI_WEBHOOK_SECRET = os.environ.get("OPENAI_WEBHOOK_SECRET")
OPENAI_AGENT_ID = os.environ.get("OPENAI_AGENT_ID")

app = modal.App("openai-agents-handler")


def verify_webhook_signature(payload: bytes, headers: dict[str, str]) -> bool:
    """Verify OpenAI webhook signature using the configured secret."""
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        client.webhooks.verify_signature(payload=payload, headers=headers)
        return True
    except Exception:
        return False


async def handle_agent_webhook(event: dict[str, Any]) -> None:
    """Handle OpenAI Agents API webhook events."""
    event_type = event.get("type")
    session_id = event.get("data", {}).get("id")

    logger.info(f"Received event: {event_type} for session {session_id}")

    if event_type == "agent.session.action_required":
        required_action = event.get("data", {}).get("required_action", {})
        if required_action.get("type") == "environment_connection":
            await handle_environment_connection(session_id)
    elif event_type == "agent.session.failed":
        await handle_session_failed(session_id)


async def handle_environment_connection(session_id: str) -> None:
    """Start or reconnect a sandbox for an agent session."""
    client = OpenAI(api_key=OPENAI_API_KEY)

    # Retrieve current session to get environment ID
    session = client.beta.agents.sessions.retrieve(session_id)
    environment_id = session.environment.id

    logger.info(f"Starting sandbox for environment {environment_id}")

    # Start a Modal Sandbox with the executor
    sandbox = await modal.Sandbox.create(
        "bash",
        "-c",
        f"""
        npm install -g @openai/codex@alpha
        mkdir -p /workspace
        CODEX_API_KEY={OPENAI_EXECUTOR_API_KEY} \\
        codex exec-server \\
          --remote https://api.openai.com/v1/agents/api \\
          --environment-id {environment_id}
        """,
        app=app,
        timeout=3600,
    )

    logger.info(f"Sandbox created: {sandbox.object_id}")


async def handle_session_failed(session_id: str) -> None:
    """Clean up when a session fails."""
    logger.info(f"Session {session_id} failed, cleaning up...")
    # Add cleanup logic here


@app.web_endpoint()
async def webhook_handler(request: dict[str, Any]) -> dict[str, str]:
    """Receive and handle OpenAI webhook events."""
    payload = json.dumps(request).encode()

    if not verify_webhook_signature(payload, {}):
        return {"error": "Invalid signature"}, 401

    try:
        await handle_agent_webhook(request)
        return {"status": "ok"}, 200
    except Exception as e:
        logger.error(f"Error handling webhook: {e}")
        return {"error": str(e)}, 500
'''

POOL_CONFIG_TEMPLATE = '''"""Generated OpenAI Agents API configuration."""

import modal

# Modal Secret containing OPENAI_API_KEY and OPENAI_EXECUTOR_API_KEY
OPENAI_SECRET_NAME = "$secret_name"

# OpenAI Agent ID (create via API or CLI)
OPENAI_AGENT_ID = "$agent_id"

# Additional Modal Secrets for the sandbox
WORKER_SECRET_NAMES = ()

# Sandbox configuration
pool = {
    "name": "$pool_name",
    "agent_id": OPENAI_AGENT_ID,
    "image": modal.Image.debian_slim(python_version="3.14")
        .apt_install("git", "nodejs", "npm", "ripgrep")
        .run_commands("npm install -g @openai/codex@alpha"),
    # Uncomment to add GPU:
    # "gpu": "A10G",
    # "cpu": 4,
    # "memory": 16384,
}
'''

EXECUTOR_WRAPPER_TEMPLATE = '''#!/usr/bin/env bash
# Simple wrapper to start the executor in a Modal sandbox

set -e

ENVIRONMENT_ID="$1"
OPENAI_EXECUTOR_API_KEY="${OPENAI_EXECUTOR_API_KEY:?Must set OPENAI_EXECUTOR_API_KEY}"

mkdir -p /workspace
cd /workspace

exec codex exec-server \\
    --remote https://api.openai.com/v1/agents/api \\
    --environment-id "$ENVIRONMENT_ID"
'''


def _ensure_dirs() -> None:
    """Create necessary directories."""
    AGENTS_DIR.mkdir(exist_ok=True)
    HOOKS_DIR.mkdir(exist_ok=True)


def _generate_pool_file(pool_name: str, agent_id: str, secret_name: str) -> Path:
    """Generate a pool configuration file."""
    _ensure_dirs()

    content = Template(POOL_CONFIG_TEMPLATE).substitute(
        pool_name=repr(pool_name),
        agent_id=repr(agent_id),
        secret_name=repr(secret_name),
    )

    pool_file = AGENTS_DIR / f"{pool_name}.py"
    pool_file.write_text(content)

    _console.print(f"✓ Created {pool_file}")
    return pool_file


def _generate_webhook_handler(pool_name: str) -> Path:
    """Generate a webhook handler file."""
    _ensure_dirs()

    handler_file = HOOKS_DIR / f"{pool_name}_handler.py"
    handler_file.write_text(WEBHOOK_HANDLER_TEMPLATE)

    _console.print(f"✓ Created {handler_file}")
    return handler_file


def _generate_executor_wrapper(pool_name: str) -> Path:
    """Generate an executor wrapper script."""
    _ensure_dirs()

    wrapper_file = AGENTS_DIR / f"{pool_name}_executor.sh"
    wrapper_file.write_text(EXECUTOR_WRAPPER_TEMPLATE)
    wrapper_file.chmod(0o755)

    _console.print(f"✓ Created {wrapper_file}")
    return wrapper_file


@app.command
def init(
    pool_name: PoolNameArg,
    *,
    api_key: ApiKeyOption | None = None,
    executor_key: ExecutorKeyOption | None = None,
    no_deploy: bool = False,
    yes: YesOption = False,
) -> None:
    """Initialize a new OpenAI Agents API pool on Modal.

    This wizard configures Modal if needed, creates the necessary secrets,
    generates pool and webhook handler files, and optionally deploys them.
    """
    _console.print(f"[bold]OpenAI Agents API on Modal[/bold] › {pool_name}\n")

    # Get API key
    if not api_key:
        api_key = _WizardPrompt.ask("OpenAI API key")

    # Create Modal Secret
    secret_name = f"openai-agents-{pool_name}"
    _console.print(f"\n[dim]Creating Modal Secret: {secret_name}[/dim]")

    try:
        subprocess.run(
            [
                "modal",
                "secret",
                "create",
                secret_name,
                f"OPENAI_API_KEY={api_key}",
                f"OPENAI_WEBHOOK_SECRET={api_key}",
            ],
            check=True,
            capture_output=True,
        )
        _console.print(f"✓ Created Modal Secret: {secret_name}")
    except subprocess.CalledProcessError:
        _console.print("[yellow]Note: Secret may already exist[/yellow]")

    # Get agent configuration
    agent_id = _WizardPrompt.ask(
        "Agent ID (from OpenAI, or leave blank to create on first session)",
        default="",
    )

    if not agent_id:
        agent_id = "agent_auto"

    # Generate files
    _console.print("\n[dim]Generating configuration...[/dim]")
    _generate_pool_file(pool_name, agent_id, secret_name)
    _generate_webhook_handler(pool_name)
    _generate_executor_wrapper(pool_name)

    # Deploy if requested
    if not no_deploy and (yes or _WizardConfirm.ask("\nDeploy now?")):
        subprocess.run(["modal", "deploy", str(HOOKS_DIR / f"{pool_name}_handler.py")])
        _console.print(f"\n✓ Deployed webhook handler for {pool_name}")

    _console.print("\n[bold]Next steps:[/bold]")
    _console.print(f"1. Review the configuration in [cyan]{AGENTS_DIR}/{pool_name}.py[/cyan]")
    _console.print(f"2. Deploy with: [cyan]modal deploy {HOOKS_DIR}/{pool_name}_handler.py[/cyan]")
    _console.print("3. Register webhook in OpenAI Project Settings")
    _console.print("   Event: agent.session.action_required")
    _console.print("4. Copy the signing secret and add to Modal Secret")


@app.command
def list() -> None:
    """List configured pools."""
    _console.print("[bold]Local pools:[/bold]\n")
    pools = sorted(AGENTS_DIR.glob("*.py"))
    for pool_file in pools:
        _console.print(f"  {pool_file.stem}")

    if not pools:
        _console.print("  (none configured yet)")
        _console.print("\nCreate one with: modal-agents init <pool-name>")


@app.command
def deploy(pool_name: PoolNameArg | None = None) -> None:
    """Deploy webhook handlers."""
    if pool_name:
        handler = HOOKS_DIR / f"{pool_name}_handler.py"
        if not handler.exists():
            _console.print(f"[red]Handler not found: {handler}[/red]")
            sys.exit(1)
        subprocess.run(["modal", "deploy", str(handler)], check=True)
    else:
        for handler in HOOKS_DIR.glob("*_handler.py"):
            _console.print(f"[dim]Deploying {handler.name}...[/dim]")
            subprocess.run(["modal", "deploy", str(handler)], check=True)


@app.command
def destroy(pool_name: PoolNameArg, yes: YesOption = False) -> None:
    """Remove a pool and its webhook handler."""
    if not yes and not _WizardConfirm.ask(
        f"Remove all resources for {pool_name}?"
    ):
        return

    handler = HOOKS_DIR / f"{pool_name}_handler.py"
    if handler.exists():
        subprocess.run(["modal", "cancel", handler.stem])
        handler.unlink()
        _console.print(f"✓ Removed handler: {handler.name}")

    pool_file = AGENTS_DIR / f"{pool_name}.py"
    if pool_file.exists():
        pool_file.unlink()
        _console.print(f"✓ Removed pool config: {pool_file.name}")

    executor = AGENTS_DIR / f"{pool_name}_executor.sh"
    if executor.exists():
        executor.unlink()
        _console.print(f"✓ Removed executor: {executor.name}")


if __name__ == "__main__":
    app()
