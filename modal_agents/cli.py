"""Configure and deploy OpenAI agent pools on Modal."""

import os
import re
import subprocess
import sys
from pathlib import Path
from string import Template
from typing import Annotated

import cyclopts
import httpx
import modal
from rich.console import Console
from rich.prompt import Confirm, Prompt

from modal_agents.pool import POOL_NAME_PATTERN, Pool, load_pool
from modal_agents.server import PENDING_SIGNING_SECRET
from modal_agents.telemetry import instrument, span
from modal_agents.templates import EXECUTOR_WRAPPER, POOL_CONFIG, WEBHOOK_HANDLER

app = cyclopts.App(name="modal-agents")
console = Console()
AGENTS_DIR = Path("agents")
HOOKS_DIR = Path("hooks")


def _validate_pool_name(_: type, value: str | None) -> None:
    if value is not None and not re.fullmatch(POOL_NAME_PATTERN, value):
        raise ValueError(
            "Use 1-40 lowercase letters, digits, or hyphens; start with a letter/digit."
        )


PoolName = Annotated[str, cyclopts.Parameter(validator=_validate_pool_name)]
Yes = Annotated[bool, cyclopts.Parameter(name=("--yes", "-y"), negative=False)]


def _pool_paths(pool_name: str) -> tuple[Path, Path, Path]:
    _validate_pool_name(str, pool_name)
    return (
        AGENTS_DIR / f"{pool_name}.py",
        HOOKS_DIR / f"{pool_name}_handler.py",
        AGENTS_DIR / f"{pool_name}_executor.sh",
    )


def _required_value(value: str | None, env_var: str, *, yes: bool, password: bool = True) -> str:
    value = value or os.environ.get(env_var)
    if value and value.strip():
        return value
    if yes or not sys.stdin.isatty():
        raise SystemExit(f"Set {env_var} or supply its command-line option.")
    value = Prompt.ask(env_var, password=password)
    if not value.strip():
        raise SystemExit(f"{env_var} cannot be empty.")
    return value


@app.command
@instrument("modal_openai.cli.init", flush=True)
def init(
    pool_name: PoolName,
    *,
    api_key: str | None = None,
    executor_key: str | None = None,
    agent_id: str | None = None,
    no_deploy: bool = False,
    yes: Yes = False,
) -> None:
    """Create an agent pool and separate Modal secrets, then optionally deploy.

    Parameters
    ----------
    api_key
        Application key; defaults to OPENAI_API_KEY.
    executor_key
        Restricted executor key; defaults to OPENAI_EXECUTOR_API_KEY.
    agent_id
        Existing OpenAI agent ID; defaults to OPENAI_AGENT_ID.
    no_deploy
        Generate files and configure secrets without deploying the handler.
    yes
        Skip prompts; both keys and an agent ID must be supplied.
    """
    paths = _pool_paths(pool_name)
    for path in paths:
        if path.exists() or path.is_symlink():
            raise SystemExit(f"File already exists: {path}")

    api_key = _required_value(api_key, "OPENAI_API_KEY", yes=yes)
    executor_key = _required_value(executor_key, "OPENAI_EXECUTOR_API_KEY", yes=yes)
    if api_key == executor_key:
        raise SystemExit("Use a separate restricted executor key, not the application key.")
    agent_id = _required_value(agent_id, "OPENAI_AGENT_ID", yes=yes, password=False)
    pool = Pool(name=pool_name, agent_id=agent_id, image=modal.Image.debian_slim())
    secrets = {
        pool.controller_secret: {"OPENAI_API_KEY": api_key},
        pool.executor_secret: {"CODEX_API_KEY": executor_key},
        pool.signing_secret: {"OPENAI_WEBHOOK_SECRET": PENDING_SIGNING_SECRET},
    }
    for name, values in secrets.items():
        # Preserve credentials on retries. Rotation is an explicit update.
        with span("modal_openai.secret.create", {"modal_openai.pool.name": pool.name}):
            modal.Secret.objects.create(name, values, allow_existing=True)
        console.print(f"✓ Modal Secret ready: {name} (existing values retained)")

    substitutions = {"pool_name": repr(pool_name), "agent_id": repr(agent_id)}
    contents = (
        Template(POOL_CONFIG).substitute(substitutions),
        Template(WEBHOOK_HANDLER).substitute(substitutions),
        EXECUTOR_WRAPPER,
    )
    for path, content in zip(paths, contents, strict=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        with span("modal_openai.config.write"), path.open("x", encoding="utf-8") as file:
            file.write(content)
        console.print(f"✓ Created {path}")
    paths[2].chmod(0o755)

    if not no_deploy and (yes or (sys.stdin.isatty() and Confirm.ask("Deploy now?"))):
        deploy(pool_name)

    console.print(
        "Register the webhook in OpenAI Project Settings, then run "
        f"modal-agents webhook-secret {pool_name} and redeploy."
    )


@app.command(name="list")
@instrument("modal_openai.cli.list", flush=True)
def list_pools() -> None:
    """List locally configured pools."""
    pools = sorted(AGENTS_DIR.glob("*.py"))
    for pool in pools:
        console.print(pool.stem, markup=False)
    if not pools:
        console.print("No pools configured. Run modal-agents init <pool-name>.")


@instrument("modal_openai.config.validate")
def _check_files(pool_name: str) -> Pool:
    config, handler, executor = _pool_paths(pool_name)
    for path in (config, handler, executor):
        if not path.is_file():
            raise ValueError(f"Required file not found: {path}")
    compile(handler.read_text(encoding="utf-8"), str(handler), "exec")
    return load_pool(config)


@app.command
@instrument("modal_openai.cli.deploy", flush=True)
def deploy(pool_name: PoolName | None = None) -> None:
    """Validate and deploy one webhook handler, or all local handlers."""
    handlers = (
        [_pool_paths(pool_name)[1]]
        if pool_name is not None
        else sorted(HOOKS_DIR.glob("*_handler.py"))
    )
    if not handlers:
        raise SystemExit("No webhook handlers found.")
    # Validate the complete batch before deploying any pool.
    for handler in handlers:
        if not handler.is_file():
            raise SystemExit(f"Handler not found: {handler}")
        _check_files(handler.name.removesuffix("_handler.py"))
    for handler in handlers:
        with span("modal_openai.app.deploy"):
            subprocess.run([sys.executable, "-m", "modal", "deploy", str(handler)], check=True)
        console.print(f"✓ Deployed {handler}")


@app.command
@instrument("modal_openai.cli.webhook_secret", flush=True)
def webhook_secret(pool_name: PoolName, *, secret: str | None = None) -> None:
    """Update the dedicated signing secret, then redeploy to apply it."""
    _validate_pool_name(str, pool_name)
    secret = _required_value(secret, "OPENAI_WEBHOOK_SECRET", yes=False)
    if secret == PENDING_SIGNING_SECRET:
        raise SystemExit("Supply the real OpenAI webhook signing secret.")
    secret_name = f"openai-agents-{pool_name}-signing"
    with span("modal_openai.secret.update"):
        modal.Secret.from_name(secret_name).update({"OPENAI_WEBHOOK_SECRET": secret})
    console.print(f"✓ Updated {secret_name}. Redeploy the handler to apply it.")


@app.command
@instrument("modal_openai.cli.destroy", flush=True)
def destroy(pool_name: PoolName, *, yes: Yes = False) -> None:
    """Stop the deployed app and remove local pool files. Keep Modal secrets."""
    paths = _pool_paths(pool_name)
    if not yes and not sys.stdin.isatty():
        raise SystemExit("destroy requires --yes when run non-interactively")
    if not yes and not Confirm.ask(f"Stop {pool_name} and remove its local files?", default=False):
        return

    app_name = f"openai-agents-{pool_name}"
    try:
        modal.App.lookup(app_name, create_if_missing=False)
    except modal.exception.NotFoundError:
        pass  # Pools initialized with --no-deploy have no deployed app.
    else:
        with span("modal_openai.app.stop"):
            subprocess.run(
                [sys.executable, "-m", "modal", "app", "stop", app_name, "--yes"], check=True
            )

    for path in paths:
        path.unlink(missing_ok=True)
    console.print(f"✓ Removed {pool_name}. Modal secrets were retained.")
    console.print("Remove its OpenAI webhook and end any remaining API sessions separately.")


@app.command
@instrument("modal_openai.cli.doctor", flush=True)
def doctor(pool_name: PoolName | None = None, *, local: bool = False) -> None:
    """Check local files, Modal secrets and deployment; --local skips cloud calls."""
    names = [pool_name] if pool_name else [path.stem for path in sorted(AGENTS_DIR.glob("*.py"))]
    if not names:
        raise SystemExit("No pools configured. Run modal-agents init <pool-name>.")
    pools = [_check_files(name) for name in names]
    if not local:
        existing = {secret.name for secret in modal.Secret.objects.list()}
        for pool in pools:
            missing = set(pool.required_secrets) - existing
            if missing:
                raise SystemExit("Missing Modal secrets: " + ", ".join(sorted(missing)))
            modal.App.lookup(pool.app_name, create_if_missing=False)
            # Remote lookup has unknown parameter/return types in Modal's generic Function.
            endpoint = modal.Function.from_name(  # pyright: ignore[reportUnknownMemberType]
                pool.app_name, "webhook"
            ).get_web_url()
            if not endpoint:
                raise SystemExit(f"{pool.name}: deployed webhook URL is missing")
            console.print(f"✓ {pool.name}: {endpoint}", markup=False)
    for pool in pools:
        console.print(f"✓ {pool.name}: local configuration is valid")
    if not local:
        console.print(
            "Secret names and deployment verified; run a live session to verify key values."
        )


def run() -> None:
    """Keep expected operational failures concise and credential-free."""
    try:
        app()
    except (modal.exception.Error, httpx.HTTPError, subprocess.CalledProcessError) as error:
        raise SystemExit(
            f"Operation failed ({type(error).__name__}). Check credentials and logs."
        ) from None
    except (OSError, ValueError, SyntaxError) as error:
        raise SystemExit(str(error)) from None
