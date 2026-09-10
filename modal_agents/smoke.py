"""Run a real session through the registered webhook, with bounded wait and cleanup."""

import asyncio
import os
from importlib.metadata import version
from typing import cast

import modal
from agent_api_sdk import AgentAPISDK, AgentAPISDKError

from modal_agents.output import TextOutput
from modal_agents.pool import Pool


async def _sandbox(app_name: str, session_id: str) -> modal.Sandbox | None:
    try:
        return await modal.Sandbox.from_name.aio(app_name, session_id)
    except modal.exception.NotFoundError:
        return None


async def _cleanup(client: AgentAPISDK, app_name: str, session_id: str) -> None:
    try:
        deleted = await client.sessions.delete(session_id)
        if not deleted.deleted or deleted.id != session_id:
            raise RuntimeError(f"API session deletion not confirmed for {session_id}")
        # Exercise the deployed cleanup path after deleting the disposable API session.
        reconcile = cast(
            modal.Function[[str], str, str],
            modal.Function.from_name(app_name, "reconcile"),  # pyright: ignore[reportUnknownMemberType]
        )
        await reconcile.remote.aio(session_id)
    finally:
        # Even if API deletion/reconciliation fails, terminate this test's sandbox.
        sandbox = await _sandbox(app_name, session_id)
        if sandbox is not None:
            async with asyncio.timeout(60):
                await sandbox.terminate.aio(wait=True)
        remaining = await _sandbox(app_name, session_id)
        if remaining is not None and await remaining.poll.aio() is None:
            raise RuntimeError(f"Sandbox cleanup not confirmed for {session_id}")


async def run_session(  # noqa: PLR0913 -- explicit client and sandbox connection settings
    *, app_name: str, agent_id: str, workspace: str, api_key: str, prompt: str, timeout: int
) -> dict[str, str]:
    """Never provision directly: the real OpenAI webhook must start the sandbox."""
    async with AgentAPISDK(api_key=api_key, timeout=timeout) as client:
        session = await client.sessions.create(
            agent_id=agent_id,
            environment={"type": "self_hosted", "workspace_directory": workspace},
        )
        print(f"Session: {session.id}", flush=True)
        output = TextOutput()
        completed = False
        try:
            async with asyncio.timeout(timeout):
                async for event in session.stream(input=prompt):
                    output.add(event)
                    if event.type in {
                        "session.turn.failed",
                        "session.turn.cancelled",
                        "session.failed",
                    }:
                        raise RuntimeError(f"{session.id}: {event.type}")
                    if event.type == "session.turn.completed":
                        completed = True
            if not completed:
                raise RuntimeError(f"{session.id}: stream ended without a completed turn")
            response = output.text
            if not response:
                raise RuntimeError(f"{session.id}: completed turn returned no response text")
            sandbox = await _sandbox(app_name, session.id)
            if sandbox is None or await sandbox.poll.aio() is not None:
                raise RuntimeError(f"{session.id}: no running webhook-created sandbox")
            result = {
                "session_id": session.id,
                "sandbox_id": sandbox.object_id,
                "response": response,
                "status": "passed",
            }
        except TimeoutError as error:
            raise RuntimeError(
                f"{session.id}: timed out waiting for the webhook/executor response"
            ) from error
        finally:
            await _cleanup(client, app_name, session.id)
            print(f"Cleaned up session and sandbox: {session.id}", flush=True)
        return result


def run_smoke(
    pool: Pool, *, api_key: str | None, api_key_secret: str | None, prompt: str, timeout: int
) -> dict[str, str]:
    """An existing application secret may run the client in a temporary Modal function."""
    if api_key_secret is None:
        if not api_key:
            raise ValueError("Supply OPENAI_API_KEY or --api-key-secret for the smoke client")
        return asyncio.run(
            run_session(
                app_name=pool.app_name,
                agent_id=pool.agent_id,
                workspace=pool.workspace,
                api_key=api_key,
                prompt=prompt,
                timeout=timeout,
            )
        )
    runner = modal.App(f"{pool.app_name}-smoke")
    image = (
        modal.Image.debian_slim()
        .pip_install(*(f"{name}=={version(name)}" for name in ("httpx", "pydantic")))
        .add_local_python_source("modal_agents", "agent_api_sdk")
    )
    # Capture only plain values; never capture a running Modal App or its client.
    app_name, agent_id, workspace = pool.app_name, pool.agent_id, pool.workspace

    @runner.function(  # pyright: ignore[reportUnknownMemberType]
        image=image,
        serialized=True,
        timeout=timeout + 180,
        secrets=[modal.Secret.from_name(api_key_secret, required_keys=["OPENAI_API_KEY"])],
    )
    async def execute() -> dict[str, str]:
        try:
            return await run_session(
                app_name=app_name,
                agent_id=agent_id,
                workspace=workspace,
                api_key=os.environ["OPENAI_API_KEY"],
                prompt=prompt,
                timeout=timeout,
            )
        except AgentAPISDKError as error:
            # Do not let remote logs print API response bodies or credential-bearing errors.
            raise RuntimeError(
                f"OpenAI request failed ({type(error).__name__}); check key/project access"
            ) from None

    with modal.enable_output(), runner.run():
        return execute.remote()
