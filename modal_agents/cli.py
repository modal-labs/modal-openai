"""Configure and deploy OpenAI agent pools on Modal."""

import os
import re
import subprocess
import sys
from pathlib import Path
from string import Template
from typing import Annotated, cast

import cyclopts
import httpx
import modal
from rich.console import Console
from rich.prompt import Confirm, Prompt

from modal_agents.checks import PENDING_SIGNING_SECRET, signing_headers
from modal_agents.pool import POOL_NAME_PATTERN, Pool, load_pool
from modal_agents.telemetry import instrument, span
from modal_agents.templates import EXECUTOR_WRAPPER, POOL_CONFIG, WEBHOOK_HANDLER

app = cyclopts.App(name="modal-agents")
console = Console()
AGENTS_DIR = Path("agents")
HOOKS_DIR = Path("hooks")
MIN_SMOKE_TIMEOUT = 30
MAX_SMOKE_TIMEOUT = 600
DEFAULT_INPUT = "Tell me your best joke in 2-3 sentences."

sdk_errors: tuple[type[Exception], ...]
try:
    from agent_api_sdk import AgentAPISDKError
except ModuleNotFoundError as error:
    if error.name != "agent_api_sdk":
        raise
    sdk_errors = ()
else:
    sdk_errors = (AgentAPISDKError,)


def list_agents(api_key: str) -> list[tuple[str, str]]:
    from modal_agents.agents_api import list_agents as execute  # noqa: PLC0415 -- optional SDK

    return execute(api_key)


def create_agent(api_key: str, name: str, model: str) -> str:
    from modal_agents.agents_api import create_agent as execute  # noqa: PLC0415 -- optional SDK

    return execute(api_key, name, model)


def run_smoke(
    pool: Pool, *, api_key: str | None, api_key_secret: str | None, prompt: str, timeout: int
) -> dict[str, str]:
    from modal_agents.smoke import run_smoke as execute  # noqa: PLC0415 -- optional SDK

    return execute(
        pool, api_key=api_key, api_key_secret=api_key_secret, prompt=prompt, timeout=timeout
    )


def _validate_pool_name(_: type, value: str | None) -> None:
    if value is not None and not re.fullmatch(POOL_NAME_PATTERN, value):
        raise ValueError(
            "Use 1-40 lowercase letters, digits, or hyphens; start with a letter/digit."
        )


PoolName = Annotated[str, cyclopts.Parameter(validator=_validate_pool_name)]
Yes = Annotated[bool, cyclopts.Parameter(name=("--yes", "-y"), negative=False)]
NoDeploy = Annotated[bool, cyclopts.Parameter(negative=False)]


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
    controller_key: str | None = None,
    create_agent_name: str | None = None,
    model: str | None = None,
    agent_id: str | None = None,
    no_deploy: NoDeploy = False,
    yes: Yes = False,
) -> None:
    """Create an agent pool and separate Modal secrets, then optionally deploy.

    Parameters
    ----------
    api_key
        Setup/application API key; defaults to OPENAI_API_KEY. Keep outside sandboxes.
    executor_key
        A second OpenAI Platform API key, restricted for executor use.
        Defaults to OPENAI_EXECUTOR_API_KEY. This is not the webhook signing secret.
    controller_key
        Optional session-read key; defaults to OPENAI_CONTROLLER_API_KEY, then api_key.
    create_agent_name
        Create a named agent instead of using agent_id; requires a model.
    model
        Model for a new agent. Required with create_agent_name in unattended setup.
    agent_id
        Existing OpenAI agent ID; defaults to OPENAI_AGENT_ID.
    no_deploy
        Generate files and configure secrets without deploying the handler.
    yes
        Skip prompts. Supply both keys and an agent ID, or create_agent_name and model.
    """
    paths = _pool_paths(pool_name)
    for path in paths:
        if path.exists() or path.is_symlink():
            raise SystemExit(f"File already exists: {path}")

    _explain_credentials()
    api_key = _required_value(api_key, "OPENAI_API_KEY", yes=yes)
    executor_key = _required_value(executor_key, "OPENAI_EXECUTOR_API_KEY", yes=yes)
    if api_key == executor_key:
        raise SystemExit("Use a separate restricted executor key, not the application key.")
    controller_key = controller_key or os.environ.get("OPENAI_CONTROLLER_API_KEY") or api_key
    if controller_key == executor_key:
        raise SystemExit("Use a separate executor key, not the controller key.")
    agent_id = _select_agent(api_key, agent_id, create_agent_name, model, yes=yes)
    pool = Pool(name=pool_name, agent_id=agent_id, image=modal.Image.debian_slim())
    secrets = {
        pool.controller_secret: {"OPENAI_API_KEY": controller_key},
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
        _finish_setup(pool_name, api_key, yes=yes)
    else:
        console.print(
            f"Next: modal-agents deploy {pool_name}, then modal-agents webhook-secret {pool_name}"
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
def webhook_secret(
    pool_name: PoolName, *, secret: str | None = None, no_deploy: NoDeploy = False
) -> None:
    """Save the signing secret, redeploy, and verify the public endpoint.

    Use --no-deploy to save the secret for a later deployment.
    """
    _validate_pool_name(str, pool_name)
    _check_files(pool_name)
    if not no_deploy:
        _webhook_instructions(pool_name)
    secret = _required_value(secret, "OPENAI_WEBHOOK_SECRET", yes=False)
    if secret == PENDING_SIGNING_SECRET:
        raise SystemExit("Supply the real OpenAI webhook signing secret.")
    if not secret.startswith("whsec_"):
        raise SystemExit("Use the whsec_ signing secret from webhook registration, not an API key.")
    try:
        signing_headers(b"{}", secret)
    except ValueError:
        raise SystemExit("The webhook signing secret is not valid base64.") from None
    secret_name = f"openai-agents-{pool_name}-signing"
    with span("modal_openai.secret.update"):
        modal.Secret.from_name(secret_name).update({"OPENAI_WEBHOOK_SECRET": secret})
    console.print(f"✓ Updated {secret_name}.")
    if no_deploy:
        console.print(
            f"Pending: modal-agents deploy {pool_name}, then modal-agents doctor {pool_name} --live"
        )
    else:
        deploy(pool_name)
        doctor(pool_name, live=True)


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
def doctor(pool_name: PoolName | None = None, *, local: bool = False, live: bool = False) -> None:
    """Check metadata; --live probes the endpoint, --local skips all cloud calls.

    A live probe verifies signed requests and the running handler. It does not prove
    OpenAI webhook registration or executor permissions; use smoke for the full flow.
    """
    if local and live:
        raise SystemExit("Choose --local or --live, not both")
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
            console.print(f"✓ {pool.name}: deployed at {endpoint}", markup=False)
            if live:
                _live_check(pool, endpoint)
    for pool in pools:
        console.print(f"✓ {pool.name}: local configuration is valid")
    if not local:
        console.print(
            "Metadata checked. Use doctor --live for endpoint readiness "
            "and smoke for end-to-end verification."
        )


def _explain_credentials() -> None:
    console.print(
        "Credentials: application and executor keys are both OpenAI Platform API keys.\n"
        "The application creates/runs sessions (and creates agents when requested).\n"
        "The controller only reads sessions: optionally supply --controller-key.\n"
        "Without it, the application key is also stored in the controller secret.\n"
        "The executor key goes inside the sandbox as CODEX_API_KEY. Create a separate\n"
        "Restricted key: List models → Read; other permissions → None.\n"
        "Different key values do not prove their scopes; verify scopes in Platform.\n"
        "The whsec_ webhook signing secret comes later, after endpoint registration.\n"
        "Keys: https://platform.openai.com/api-keys"
    )


def _select_agent(
    api_key: str, agent_id: str | None, create_name: str | None, model: str | None, *, yes: bool
) -> str:
    agent_id = agent_id or os.environ.get("OPENAI_AGENT_ID")
    if agent_id and create_name:
        raise SystemExit("Choose an existing agent ID or --create-agent-name, not both")
    if agent_id:
        return agent_id
    if create_name:
        if not model:
            raise SystemExit("--create-agent-name requires --model")
        return create_agent(api_key, create_name, model)
    if yes or not sys.stdin.isatty():
        raise SystemExit(
            "Set OPENAI_AGENT_ID, supply --agent-id, or use --create-agent-name and --model."
        )
    agents = list_agents(api_key)
    for number, (identifier, name) in enumerate(agents, 1):
        console.print(f"{number}. {name} ({identifier})", markup=False)
    console.print("new. Create an agent\nmanual. Enter an existing agent ID")
    choice = Prompt.ask(
        "Select the agent this pool will serve",
        choices=[str(i) for i in range(1, len(agents) + 1)] + ["new", "manual"],
    )
    if choice == "manual":
        return _required_value(None, "OPENAI_AGENT_ID", yes=False, password=False)
    if choice == "new":
        name = Prompt.ask("Agent name").strip()
        model_name = Prompt.ask("Model for the agent (for example gpt-5.5)").strip()
        if not name or not model_name:
            raise SystemExit("Agent name and model cannot be empty")
        return create_agent(api_key, name, model_name)
    return agents[int(choice) - 1][0]


def _endpoint(pool: Pool) -> str:
    endpoint = modal.Function.from_name(  # pyright: ignore[reportUnknownMemberType]
        pool.app_name, "webhook"
    ).get_web_url()
    if not endpoint:
        raise SystemExit("Deployed webhook URL is missing; deploy this pool first")
    return endpoint


def _webhook_instructions(pool_name: str) -> None:
    pool = _check_files(pool_name)
    console.print(f"Webhook URL: {_endpoint(pool)}", markup=False)
    console.print(
        "Open https://platform.openai.com/settings/project/webhooks in the SAME project\n"
        "as the application key and agent. Register agent.session.action_required and\n"
        "agent.session.failed. Copy the whsec_ signing secret.\n"
        f"Sessions must use agent_id={pool.agent_id}; inline agents will not match this pool.",
        markup=False,
    )


def _finish_setup(pool_name: str, api_key: str, *, yes: bool) -> None:
    _webhook_instructions(pool_name)
    secret = os.environ.get("OPENAI_WEBHOOK_SECRET")
    if not secret and not yes and sys.stdin.isatty():
        secret = Prompt.ask(
            "Webhook signing secret (blank to resume later)", password=True, default=""
        )
    if not secret:
        console.print(
            f"Setup pending: run modal-agents webhook-secret {pool_name} after registration."
        )
        return
    webhook_secret(pool_name, secret=secret)
    if (
        not yes
        and sys.stdin.isatty()
        and Confirm.ask(
            "Run a live smoke test now? It uses API/Modal credits "
            "and cleans up its session and sandbox.",
            default=True,
        )
    ):
        smoke(pool_name, api_key=api_key)


def _live_check(pool: Pool, endpoint: str) -> None:
    try:
        check = cast(
            modal.Function[[str], dict[str, str], dict[str, str]],
            modal.Function.from_name(pool.app_name, "check_webhook"),  # pyright: ignore[reportUnknownMemberType]
        )
        result = check.remote(endpoint)
    except modal.exception.NotFoundError:
        raise SystemExit(
            f"{pool.name}: readiness function missing; redeploy the current handler"
        ) from None
    if result.get("status") != "ready":
        detail = result.get("detail", "Unexpected readiness result")
        raise SystemExit(
            f"{pool.name}: NOT READY ({detail}). Check Modal logs, signing secret, and deployment. "
            "A stale container may still serve the previous handler; inspect before replacing it."
        )
    console.print(
        f"✓ {pool.name}: live endpoint ready (signed challenge passed; unsigned request rejected)"
    )
    console.print("Webhook registration and executor connectivity still require a live smoke test.")


@app.command
@instrument("modal_openai.cli.smoke", flush=True)
def smoke(
    pool_name: PoolName,
    *,
    api_key: str | None = None,
    api_key_secret: str | None = None,
    prompt: str = DEFAULT_INPUT,
    timeout: int = 300,
) -> None:
    """Test real webhook delivery, sandbox startup, nonempty output, and cleanup.

    Supply an application key with session write access via OPENAI_API_KEY/private
    prompt, or --api-key-secret naming an existing Modal secret with OPENAI_API_KEY.
    The secret option runs a temporary client in Modal; no key is downloaded.
    This command creates billable API/Modal work. It never provisions an executor
    directly: the registered OpenAI webhook must start the sandbox.
    """
    if not MIN_SMOKE_TIMEOUT <= timeout <= MAX_SMOKE_TIMEOUT:
        raise SystemExit("timeout must be between 30 and 600 seconds")
    if not prompt.strip():
        raise SystemExit("prompt cannot be empty")
    if api_key and api_key_secret:
        raise SystemExit("Choose --api-key or --api-key-secret")
    pool = _check_files(pool_name)
    doctor(pool_name, live=True)
    if not api_key_secret:
        api_key = _required_value(api_key, "OPENAI_API_KEY", yes=False)
    result = run_smoke(
        pool, api_key=api_key, api_key_secret=api_key_secret, prompt=prompt, timeout=timeout
    )
    console.print(
        f"✓ E2E passed: {result['session_id']} → {result['sandbox_id']}; cleanup confirmed",
        markup=False,
    )
    console.print(result["response"], markup=False)


def run() -> None:
    """Keep expected operational failures concise and credential-free."""
    try:
        app()
    except (  # noqa: RUF005 -- mypy cannot type-check starred exception tuples
        modal.exception.Error,
        httpx.HTTPError,
        subprocess.CalledProcessError,
    ) + sdk_errors as error:
        raise SystemExit(
            f"Operation failed ({type(error).__name__}). Check credentials and logs."
        ) from None
    except ModuleNotFoundError as error:
        if error.name != "agent_api_sdk":
            raise
        raise SystemExit(
            "This command requires the Agents API preview SDK. Install it with: "
            'python -m pip install "agent-api-sdk @ git+https://github.com/'
            "OpenAI-Early-Access/agents-api-python-preview.git@"
            '076c5f3fb9dbad9a1096a77943163fbc77061a0d"'
        ) from None
    except (OSError, ValueError, SyntaxError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
