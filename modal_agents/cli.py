"""Scaffold and deploy OpenAI webhook handlers on Modal."""

import os
import re
import subprocess
import sys
from pathlib import Path
from string import Template
from typing import Annotated

import cyclopts
import modal
from rich.console import Console
from rich.prompt import Confirm, Prompt

from modal_agents.templates import EXECUTOR_WRAPPER, POOL_CONFIG, WEBHOOK_HANDLER

app = cyclopts.App(name="modal-agents")
console = Console()
AGENTS_DIR = Path("agents")
HOOKS_DIR = Path("hooks")


def _validate_pool_name(value: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise ValueError("Use letters, digits, hyphens, or underscores; start with a letter or digit.")


PoolName = Annotated[str, cyclopts.Parameter(validator=_validate_pool_name)]
Yes = Annotated[bool, cyclopts.Parameter(name=("--yes", "-y"), negative=False)]


def _pool_paths(pool_name: str) -> tuple[Path, Path, Path]:
    return (
        AGENTS_DIR / f"{pool_name}.py",
        HOOKS_DIR / f"{pool_name}_handler.py",
        AGENTS_DIR / f"{pool_name}_executor.sh",
    )


def _api_key(value: str | None, env_var: str, *, yes: bool) -> str:
    value = value or os.environ.get(env_var)
    if value:
        return value
    if yes:
        raise SystemExit(f"Set {env_var} or supply its command-line option.")
    return Prompt.ask(env_var, password=True)


@app.command
def init(
    pool_name: PoolName,
    *,
    api_key: str | None = None,
    executor_key: str | None = None,
    no_deploy: bool = False,
    yes: Yes = False,
) -> None:
    """Create a pool scaffold and Modal Secret, then optionally deploy.

    Parameters
    ----------
    api_key
        OpenAI API key; defaults to OPENAI_API_KEY.
    executor_key
        Restricted executor key; defaults to OPENAI_EXECUTOR_API_KEY.
    no_deploy
        Generate files without deploying the handler.
    yes
        Skip prompts; both API keys must be supplied.
    """
    paths = _pool_paths(pool_name)
    for path in paths:
        if path.exists():
            raise SystemExit(f"File already exists: {path}")

    keys = {
        "OPENAI_API_KEY": _api_key(api_key, "OPENAI_API_KEY", yes=yes),
        "OPENAI_EXECUTOR_API_KEY": _api_key(
            executor_key, "OPENAI_EXECUTOR_API_KEY", yes=yes
        ),
    }
    agent_id = "" if yes else Prompt.ask("Existing OpenAI agent ID (optional)", default="")
    secret_name = f"openai-agents-{pool_name}"
    modal.Secret.objects.create(secret_name, keys)
    console.print(f"✓ Created Modal Secret: {secret_name}")

    substitutions = {
        "pool_name": repr(pool_name),
        "agent_id": repr(agent_id),
        "secret_name": repr(secret_name),
    }
    contents = (
        Template(POOL_CONFIG).substitute(substitutions),
        Template(WEBHOOK_HANDLER).substitute(substitutions),
        EXECUTOR_WRAPPER,
    )
    for path, content in zip(paths, contents, strict=True):
        path.parent.mkdir(exist_ok=True)
        with path.open("x", encoding="utf-8") as file:
            file.write(content)
        console.print(f"✓ Created {path}")
    paths[2].chmod(0o755)

    if not no_deploy and (yes or Confirm.ask("Deploy now?")):
        deploy(pool_name)

    console.print("Sandbox lifecycle handling still needs to be implemented in the handler.")
    console.print(
        "Register the webhook in OpenAI Project Settings, then run "
        f"modal-agents webhook-secret {pool_name} and redeploy."
    )


@app.command(name="list")
def list_pools() -> None:
    """List locally configured pools."""
    pools = sorted(AGENTS_DIR.glob("*.py"))
    for pool in pools:
        console.print(pool.stem)
    if not pools:
        console.print("No pools configured. Run modal-agents init <pool-name>.")


@app.command
def deploy(pool_name: PoolName | None = None) -> None:
    """Deploy one webhook handler, or all local handlers."""
    handlers = (
        [_pool_paths(pool_name)[1]]
        if pool_name is not None
        else sorted(HOOKS_DIR.glob("*_handler.py"))
    )
    if not handlers:
        raise SystemExit("No webhook handlers found.")
    for handler in handlers:
        if not handler.is_file():
            raise SystemExit(f"Handler not found: {handler}")
        subprocess.run([sys.executable, "-m", "modal", "deploy", str(handler)], check=True)
        console.print(f"✓ Deployed {handler}")


@app.command
def webhook_secret(pool_name: PoolName, *, secret: str | None = None) -> None:
    """Update the signing secret without replacing the pool's other keys."""
    secret = secret or Prompt.ask("OpenAI webhook signing secret", password=True)
    secret_name = f"openai-agents-{pool_name}"
    modal.Secret.from_name(secret_name).update({"OPENAI_WEBHOOK_SECRET": secret})
    console.print(f"✓ Updated {secret_name}. Redeploy the handler to apply it.")


@app.command
def destroy(pool_name: PoolName, *, yes: Yes = False) -> None:
    """Stop the deployed app and remove local pool files. Keep the Modal Secret."""
    if not yes and not Confirm.ask(f"Stop {pool_name} and remove its local files?"):
        return

    app_name = f"openai-agents-{pool_name}"
    try:
        modal.App.lookup(app_name)
    except modal.exception.NotFoundError:
        pass  # Pools initialized with --no-deploy have no deployed app.
    else:
        subprocess.run(
            [sys.executable, "-m", "modal", "app", "stop", app_name, "--yes"],
            check=True,
        )

    for path in _pool_paths(pool_name):
        path.unlink(missing_ok=True)
    console.print(f"✓ Removed {pool_name}. Modal Secret {app_name} was retained.")
