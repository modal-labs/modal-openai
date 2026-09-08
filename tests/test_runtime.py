"""Behavioral coverage at the HTTP and Modal boundaries; no live credentials."""

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import modal
import pytest
from pydantic import ValidationError

from modal_agents import runtime
from modal_agents.pool import Pool


def session_payload(**changes):
    return {
        "id": "session_test",
        "agent": {"id": "agent_test"},
        "status": "requires_action",
        "environment": {
            "type": "self_hosted",
            "environment_id": "env_test",
            "workspace_directory": "/workspace",
        },
        "required_actions": [{"type": "environment_connection", "environment_id": "env_test"}],
    } | changes


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "application-secret")
    pool = Pool(
        name="test",
        agent_id="agent_test",
        image=modal.Image.debian_slim(),
        gpu="A10G",
        cpu=4,
        memory=8192,
        worker_secret_names=("data",),
    )
    retrieve = AsyncMock(return_value=runtime.Session.model_validate(session_payload()))
    sandbox = Mock(
        object_id="sb-test", detach=Mock(aio=AsyncMock()), terminate=Mock(aio=AsyncMock())
    )
    lookup = AsyncMock(side_effect=modal.exception.NotFoundError("absent"))
    create = AsyncMock(return_value=sandbox)
    secret = Mock(side_effect=lambda name, **kwargs: name)
    monkeypatch.setattr(runtime, "retrieve_session", retrieve)
    monkeypatch.setattr(modal.Sandbox, "from_name", Mock(aio=lookup))
    monkeypatch.setattr(modal.Sandbox, "create", Mock(aio=create))
    monkeypatch.setattr(modal.Secret, "from_name", secret)
    return pool, retrieve, sandbox, lookup, create


def reconcile(boundary):
    pool = boundary[0]
    return asyncio.run(
        runtime.reconcile_session(pool, modal.App(pool.app_name), pool.image, "session_test")
    )


def test_start_uses_pool_and_only_executor_secrets(boundary):
    pool, _, sandbox, lookup, create = boundary
    assert reconcile(boundary) == "started"
    lookup.assert_awaited_once_with(pool.app_name, "session_test")
    args, kwargs = create.call_args
    assert args == ("bash", "/opt/modal-agents/executor.sh", "env_test")
    assert kwargs["name"] == "session_test"
    assert kwargs["secrets"] == [pool.executor_secret, "data"]
    assert kwargs["cpu"] == 4 and kwargs["memory"] == 8192 and kwargs["gpu"] == "A10G"
    assert kwargs["timeout"] == 1800
    assert "application-secret" not in repr(create.call_args)
    sandbox.detach.aio.assert_awaited_once()


def test_duplicate_delivery_reuses_running_sandbox(boundary):
    _, _, sandbox, lookup, create = boundary
    lookup.side_effect = None
    lookup.return_value = sandbox
    assert reconcile(boundary) == "already_running"
    create.assert_not_awaited()
    sandbox.detach.aio.assert_awaited_once()


@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("exists", [False, True])
def test_cleanup_failed_or_deleted_session(boundary, deleted, exists):
    _, retrieve, sandbox, lookup, create = boundary
    retrieve.return_value = (
        None if deleted else runtime.Session.model_validate(session_payload(status="failed"))
    )
    if exists:
        lookup.side_effect = None
        lookup.return_value = sandbox
    assert reconcile(boundary) == ("terminated" if exists else "absent")
    create.assert_not_awaited()
    assert sandbox.terminate.aio.await_count == int(exists)
    assert sandbox.detach.aio.await_count == int(exists)


@pytest.mark.parametrize(
    "changes,outcome",
    [
        ({"agent": {"id": "agent_other"}}, "ignored_agent"),
        ({"environment": {"type": "none"}}, "ignored_environment"),
        ({"status": "idle", "required_actions": []}, "no_action"),
        ({"required_actions": [{"type": "function_call"}]}, "no_action"),
    ],
)
def test_stale_or_unrelated_events_do_not_start_sandbox(boundary, changes, outcome):
    boundary[1].return_value = runtime.Session.model_validate(session_payload(**changes))
    assert reconcile(boundary) == outcome
    boundary[4].assert_not_awaited()
    if outcome == "ignored_agent":
        boundary[3].assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"required_actions": [{"type": "environment_connection", "environment_id": "env_other"}]},
        {"environment": {"type": "self_hosted"}},
        {
            "environment": {
                "type": "self_hosted",
                "environment_id": "env_test",
                "workspace_directory": "/other",
            }
        },
    ],
)
def test_inconsistent_environment_is_rejected(boundary, changes):
    boundary[1].return_value = runtime.Session.model_validate(session_payload(**changes))
    with pytest.raises(ValueError):
        reconcile(boundary)
    boundary[4].assert_not_awaited()


def test_creation_race_reuses_winner(boundary):
    _, _, sandbox, lookup, create = boundary
    lookup.side_effect = [modal.exception.NotFoundError("absent"), sandbox]
    create.side_effect = modal.exception.AlreadyExistsError("race")
    assert reconcile(boundary) == "already_running"
    sandbox.detach.aio.assert_awaited_once()


def test_creation_race_without_winner_retries(boundary):
    boundary[4].side_effect = modal.exception.AlreadyExistsError("race")
    with pytest.raises(modal.exception.AlreadyExistsError):
        reconcile(boundary)


def test_provision_failure_propagates_for_retry(boundary):
    boundary[4].side_effect = modal.exception.Error("temporary")
    with pytest.raises(modal.exception.Error):
        reconcile(boundary)


def test_cleanup_failure_propagates_and_detaches(boundary):
    _, retrieve, sandbox, lookup, _ = boundary
    retrieve.return_value = None
    lookup.side_effect = None
    lookup.return_value = sandbox
    sandbox.terminate.aio.side_effect = modal.exception.Error("temporary")
    with pytest.raises(modal.exception.Error):
        reconcile(boundary)
    sandbox.detach.aio.assert_awaited_once()


def test_api_failure_has_no_sandbox_side_effects(boundary):
    boundary[1].side_effect = httpx.ConnectError("offline")
    with pytest.raises(httpx.ConnectError):
        reconcile(boundary)
    boundary[3].assert_not_awaited()
    boundary[4].assert_not_awaited()


@pytest.mark.parametrize(
    "status,payload,expected_error",
    [
        (200, session_payload(), None),
        (200, session_payload(environment={"type": "self_hosted", "id": "env_test"}), None),
        (404, {}, None),
        (429, {}, httpx.HTTPStatusError),
        (500, {}, httpx.HTTPStatusError),
        (200, {}, ValidationError),
        (200, session_payload(id="different"), ValueError),
    ],
)
def test_session_http_contract(status, payload, expected_error):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json=payload)

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await runtime.retrieve_session(client, "session_test")

    if expected_error:
        with pytest.raises(expected_error):
            asyncio.run(fetch())
    else:
        result = asyncio.run(fetch())
        assert (result is None) == (status == 404)
    assert str(requests[0].url) == "https://api.openai.com/v1/agents/sessions/session_test"
