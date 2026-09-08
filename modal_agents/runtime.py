"""Reconcile preview Agents API session state with named Modal sandboxes.

The contract follows OpenAI's preview SDK 0.3.1, revision 076c5f3.
Only this module depends on the preview session response shape.
"""

import logging
import os
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import cast
from urllib.parse import quote

import httpx
import modal
from pydantic import AliasChoices, BaseModel, Field

from modal_agents.pool import Pool
from modal_agents.telemetry import (
    instrument_async,
    instrument_httpx,
    set_attribute,
    span,
)

logger = logging.getLogger(__name__)
API_ROOT = "https://api.openai.com/v1/agents"


class Agent(BaseModel):
    id: str


class Environment(BaseModel):
    type: str
    environment_id: str | None = Field(
        default=None, validation_alias=AliasChoices("environment_id", "id")
    )
    workspace_directory: str = "/workspace"


class RequiredAction(BaseModel):
    type: str
    environment_id: str | None = None


class Session(BaseModel):
    id: str
    agent: Agent
    status: str
    environment: Environment
    required_actions: list[RequiredAction] = Field(default_factory=list)


@instrument_async("modal_openai.session.retrieve")
async def retrieve_session(client: httpx.AsyncClient, session_id: str) -> Session | None:
    response = await client.get(f"{API_ROOT}/sessions/{quote(session_id, safe='')}")
    if response.status_code == HTTPStatus.NOT_FOUND:
        return None
    response.raise_for_status()
    session = Session.model_validate(response.json())
    if session.id != session_id:
        raise ValueError("Session response ID did not match the requested session")
    return session


@instrument_async("modal_openai.sandbox.find")
async def find_sandbox(pool: Pool, session_id: str) -> modal.Sandbox | None:
    try:
        return await modal.Sandbox.from_name.aio(pool.app_name, session_id)
    except modal.exception.NotFoundError:
        return None


async def reconcile_session(pool: Pool, app: modal.App, image: modal.Image, session_id: str) -> str:
    """Re-read authoritative state on every delivery, including retries."""
    with span(
        "modal_openai.session.reconcile",
        {"modal_openai.pool.name": pool.name, "gen_ai.conversation.id": session_id},
    ) as current:
        outcome = await _reconcile_session(pool, app, image, session_id)
        set_attribute(current, "modal_openai.outcome", outcome)
        return outcome


async def _detach(sandbox: modal.Sandbox) -> None:
    # Modal 1.5.5 omits the return annotation on detach's async wrapper.
    detach = cast(Callable[[], Awaitable[None]], sandbox.detach.aio)
    with span("modal_openai.sandbox.detach"):
        await detach()


async def _reconcile_session(
    pool: Pool, app: modal.App, image: modal.Image, session_id: str
) -> str:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, timeout=30
    ) as client:
        instrument_httpx(client)
        session = await retrieve_session(client, session_id)

    # An endpoint may receive events for every agent in the OpenAI project.
    if session is not None and session.agent.id != pool.agent_id:
        return "ignored_agent"

    sandbox = await find_sandbox(pool, session_id)
    try:
        if session is None or session.status == "failed":
            if sandbox is not None:
                with span(
                    "modal_openai.sandbox.terminate", {"modal_openai.sandbox.id": sandbox.object_id}
                ):
                    await sandbox.terminate.aio()
            logger.info("Session cleanup: pool=%s session=%s", pool.name, session_id)
            return "terminated" if sandbox is not None else "absent"
        if session.environment.type != "self_hosted":
            return "ignored_environment"
        actions = [
            action for action in session.required_actions if action.type == "environment_connection"
        ]
        if not actions:
            return "no_action"
        if sandbox is not None:
            return "already_running"
        environment_id = session.environment.environment_id
        if not environment_id or any(action.environment_id != environment_id for action in actions):
            raise ValueError("Environment connection action does not match session environment")
        if session.environment.workspace_directory != pool.workspace:
            raise ValueError("Session workspace_directory must match the pool workspace")
        sandbox, outcome = await _provision(pool, app, image, session_id, environment_id)
        return outcome
    finally:
        if sandbox is not None:
            await _detach(sandbox)


async def _provision(
    pool: Pool, app: modal.App, image: modal.Image, session_id: str, environment_id: str
) -> tuple[modal.Sandbox, str]:
    try:
        with span("modal_openai.sandbox.create") as current:
            # Modal's signature includes unparameterized PathLike in unused options.
            sandbox = await modal.Sandbox.create.aio(  # pyright: ignore[reportUnknownMemberType]
                "bash",
                "/opt/modal-agents/executor.sh",
                environment_id,
                app=app,
                name=session_id,
                image=image,
                secrets=[
                    modal.Secret.from_name(pool.executor_secret, required_keys=["CODEX_API_KEY"]),
                    *(modal.Secret.from_name(name) for name in pool.worker_secret_names),
                ],
                env={"MODAL_AGENTS_WORKSPACE": pool.workspace},
                workdir=pool.workspace,
                cpu=pool.cpu,
                memory=pool.memory,
                gpu=pool.gpu,
                timeout=pool.timeout,
            )
            set_attribute(current, "modal_openai.sandbox.id", sandbox.object_id)
    except modal.exception.AlreadyExistsError:
        # A deployment overlap or retried create can race a previous worker.
        existing = await find_sandbox(pool, session_id)
        if existing is None:
            raise
        return existing, "already_running"
    logger.info(
        "Sandbox started: pool=%s session=%s sandbox=%s", pool.name, session_id, sandbox.object_id
    )
    return sandbox, "started"
