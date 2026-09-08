"""CLI for deploying OpenAI Agents API on Modal sandboxes."""

from __future__ import annotations

import os
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

import modal
from fastapi import HTTPException, Request
from openai import InvalidWebhookSignatureError, OpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = modal.App("openai-agents-$pool_name")

webhook_image = (
    modal.Image.debian_slim(python_version="3.13")
    .pip_install(
        "fastapi>=0.115.0",
        "openai>=1.92.0",
    )
)

secrets = modal.Secret.from_dict(
    {
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
        "OPENAI_WEBHOOK_SECRET": os.environ.get("OPENAI_WEBHOOK_SECRET", ""),
    }
)


def verify_webhook_signature(raw: bytes, headers: dict[str, str]) -> bool:
    """Verify OpenAI webhook signature."""
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        webhook_secret=os.environ["OPENAI_WEBHOOK_SECRET"],
    )
    try:
        client.webhooks.verify_signature(raw, headers)
        return True
    except InvalidWebhookSignatureError:
        return False


@app.function(image=webhook_image, secrets=[secrets], timeout=600)
@modal.fastapi_endpoint(method="POST")
async def webhook(request: Request) -> dict[str, str]:
    """Receive and handle OpenAI webhook events."""
    raw = await request.body()
    headers = {k: v for k, v in request.headers.items()}

    if not verify_webhook_signature(raw, headers):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("type")
    logger.info(f"Received {event_type} event")

    # TODO: Implement event handling
    # - agent.session.created: Start sandbox
    # - agent.session.failed: Cleanup sandbox

    return {"status": "ok", "event_type": event_type}
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
        pool_name=pool_name,
        agent_id=agent_id,
        secret_name=secret_name,
    )

    pool_file = AGENTS_DIR / f"{pool_name}.py"
    pool_file.write_text(content)

    _console.print(f"✓ Created {pool_file}")
    return pool_file


def _generate_webhook_handler(pool_name: str) -> Path:
    """Generate a webhook handler file."""
    _ensure_dirs()

    content = Template(WEBHOOK_HANDLER_TEMPLATE).substitute(pool_name=pool_name)
    handler_file = HOOKS_DIR / f"{pool_name}_handler.py"
    handler_file.write_text(content)

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
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            if yes:
                _console.print("[red]Error: API key required. Set OPENAI_API_KEY env var[/red]")
                sys.exit(1)
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
    if yes:
        agent_id = "agent_auto"
    else:
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
def webhook_secret(
    pool_name: PoolNameArg,
    secret: str | None = None,
) -> None:
    """Update webhook signing secret for a pool.

    After creating the webhook in OpenAI Project Settings, run this command
    to add the signing secret to the Modal Secret.

    Example:
        modal-agents webhook-secret my-agent --secret sk_live_...
    """
    secret_name = f"openai-agents-{pool_name}"

    if not secret:
        secret = _WizardPrompt.ask(
            "Webhook signing secret from OpenAI (sk_live_...)"
        )

    try:
        subprocess.run(
            [
                "modal",
                "secret",
                "create",
                secret_name,
                f"OPENAI_WEBHOOK_SECRET={secret}",
                "--force",
            ],
            check=True,
            capture_output=True,
        )
        _console.print(f"✓ Updated {secret_name}")
        _console.print(f"\nWebhook is now ready to receive events from OpenAI")
    except subprocess.CalledProcessError as e:
        _console.print(f"[red]Error updating secret: {e}[/red]")
        sys.exit(1)


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
