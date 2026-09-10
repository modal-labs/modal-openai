"""Smoke must prove output and webhook provisioning, and clean up on every exit."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import modal
import pytest

sdk = pytest.importorskip("agent_api_sdk")
SessionTurnOutputTextDeltaEvent = sdk.SessionTurnOutputTextDeltaEvent
SessionTurnOutputTextDoneEvent = sdk.SessionTurnOutputTextDoneEvent
smoke = pytest.importorskip("modal_agents.smoke")


def delta(text):
    return SessionTurnOutputTextDeltaEvent(
        type="session.turn.output_text.delta",
        event_id="event_1",
        session_id="sess_test",
        item_id="item_1",
        output_index=0,
        content_index=0,
        delta=text,
    )


@pytest.fixture
def setup(monkeypatch):
    sandbox = SimpleNamespace(
        object_id="sb_test", poll=SimpleNamespace(aio=AsyncMock(return_value=None))
    )
    state = {"sandbox": sandbox}

    async def find(*args):
        if state["sandbox"] is None:
            raise modal.exception.NotFoundError("absent")
        return state["sandbox"]

    async def terminate(*, wait):
        assert wait is True
        state["sandbox"] = None

    async def reconcile(session_id):
        assert session_id == "sess_test"
        state["sandbox"] = None
        return "terminated"

    sandbox.terminate = SimpleNamespace(aio=AsyncMock(side_effect=terminate))
    session = SimpleNamespace(id="sess_test")
    api = SimpleNamespace(
        sessions=SimpleNamespace(
            create=AsyncMock(return_value=session),
            delete=AsyncMock(return_value=SimpleNamespace(id="sess_test", deleted=True)),
        )
    )
    context = AsyncMock()
    context.__aenter__.return_value = api
    monkeypatch.setattr(smoke, "AgentAPISDK", Mock(return_value=context))
    monkeypatch.setattr(
        modal.Sandbox, "from_name", SimpleNamespace(aio=AsyncMock(side_effect=find))
    )
    provision = Mock(side_effect=AssertionError("Smoke cannot provision directly"))
    monkeypatch.setattr(modal.Sandbox, "create", provision)
    worker = SimpleNamespace(remote=SimpleNamespace(aio=AsyncMock(side_effect=reconcile)))
    monkeypatch.setattr(modal.Function, "from_name", Mock(return_value=worker))
    return api, session, sandbox, state, worker, provision


def run(session, events, timeout=30):
    async def stream(**kwargs):
        assert kwargs["input"] == "Tell a joke"
        for event in events:
            if isinstance(event, Exception):
                raise event
            yield event

    session.stream = stream
    return asyncio.run(
        smoke.run_session(
            app_name="test",
            agent_id="agent_test",
            workspace="/workspace",
            api_key="key",
            prompt="Tell a joke",
            timeout=timeout,
        )
    )


def test_smoke_captures_typed_delta_checks_sandbox_and_cleans_up(setup):
    api, session, _, state, worker, provision = setup
    result = run(session, [delta("Actual joke"), SimpleNamespace(type="session.turn.completed")])
    assert result == {
        "session_id": "sess_test",
        "sandbox_id": "sb_test",
        "response": "Actual joke",
        "status": "passed",
    }
    api.sessions.create.assert_awaited_once_with(
        agent_id="agent_test",
        environment={"type": "self_hosted", "workspace_directory": "/workspace"},
    )
    api.sessions.delete.assert_awaited_once_with("sess_test")
    worker.remote.aio.assert_awaited_once_with("sess_test")
    assert state["sandbox"] is None
    provision.assert_not_called()


@pytest.mark.parametrize(
    "events, message",
    [
        ([SimpleNamespace(type="session.turn.completed")], "no response text"),
        ([delta("   "), SimpleNamespace(type="session.turn.completed")], "no response text"),
        ([delta("partial")], "without a completed"),
        ([SimpleNamespace(type="session.turn.failed")], "session.turn.failed"),
        ([SimpleNamespace(type="session.turn.cancelled")], "session.turn.cancelled"),
        ([SimpleNamespace(type="session.failed")], "session.failed"),
        ([TimeoutError("timed out")], "timed out"),
    ],
)
def test_failure_always_attempts_cleanup(setup, events, message):
    api, session, _, state, _, _ = setup
    with pytest.raises((RuntimeError, TimeoutError), match=message):
        run(session, events)
    api.sessions.delete.assert_awaited_once_with("sess_test")
    assert state["sandbox"] is None


@pytest.mark.parametrize("exited", [False, True])
def test_completed_response_without_running_sandbox_is_not_success(setup, exited):
    _, session, sandbox, state, _, _ = setup
    if exited:
        sandbox.poll.aio.return_value = 1
    else:
        state["sandbox"] = None
    with pytest.raises(RuntimeError, match="no running"):
        run(session, [delta("joke"), SimpleNamespace(type="session.turn.completed")])


def test_api_delete_failure_still_terminates_sandbox_and_fails(setup):
    api, session, sandbox, state, _, _ = setup
    api.sessions.delete.side_effect = RuntimeError("delete failed")
    with pytest.raises(RuntimeError, match="delete failed"):
        run(session, [delta("joke"), SimpleNamespace(type="session.turn.completed")])
    sandbox.terminate.aio.assert_awaited_once()
    assert state["sandbox"] is None


def test_reconciliation_failure_uses_fallback_cleanup_and_fails(setup):
    _, session, sandbox, state, worker, _ = setup
    worker.remote.aio.side_effect = RuntimeError("worker failed")
    with pytest.raises(RuntimeError, match="worker failed"):
        run(session, [delta("joke"), SimpleNamespace(type="session.turn.completed")])
    sandbox.terminate.aio.assert_awaited_once()
    assert state["sandbox"] is None


def test_unconfirmed_cleanup_is_failure(setup):
    _, session, sandbox, _, worker, _ = setup
    worker.remote.aio.side_effect = None
    sandbox.terminate.aio.side_effect = None
    with pytest.raises(RuntimeError, match="cleanup not confirmed"):
        run(session, [delta("joke"), SimpleNamespace(type="session.turn.completed")])


def test_local_smoke_requires_application_key():
    pool = Mock(app_name="test", agent_id="agent_test", workspace="/workspace")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        smoke.run_smoke(pool, api_key=None, api_key_secret=None, prompt="test", timeout=30)


def test_local_smoke_passes_pool_settings(monkeypatch):
    pool = Mock(app_name="test", agent_id="agent_test", workspace="/workspace")
    run_session = AsyncMock(return_value={"status": "passed"})
    monkeypatch.setattr(smoke, "run_session", run_session)
    assert smoke.run_smoke(
        pool, api_key="key", api_key_secret=None, prompt="hello", timeout=35
    ) == {"status": "passed"}
    assert run_session.call_args.kwargs["api_key"] == "key"
    assert run_session.call_args.kwargs["agent_id"] == "agent_test"


def test_done_only_live_delivery_is_a_success(setup):

    _, session, _, _, _, _ = setup
    done = SessionTurnOutputTextDoneEvent(
        type="session.turn.output_text.done",
        event_id="event_done",
        session_id="sess_test",
        item_id="msg_1",
        output_index=0,
        content_index=0,
        text="The complete joke",
    )
    result = run(session, [done, SimpleNamespace(type="session.turn.completed")])
    assert result["response"] == "The complete joke"


def test_unconfirmed_api_delete_is_not_success(setup):
    api, session, sandbox, _, _, _ = setup
    api.sessions.delete.return_value = SimpleNamespace(id="sess_test", deleted=False)
    with pytest.raises(RuntimeError, match="API session deletion not confirmed"):
        run(session, [delta("joke"), SimpleNamespace(type="session.turn.completed")])
    sandbox.terminate.aio.assert_awaited_once()


def test_terminated_sandbox_can_remain_resolvable_by_name(setup):
    _, session, sandbox, _, worker, _ = setup
    worker.remote.aio.side_effect = None

    async def terminate(*, wait):
        assert wait is True
        sandbox.poll.aio.return_value = -15

    sandbox.terminate.aio.side_effect = terminate
    result = run(session, [delta("joke"), SimpleNamespace(type="session.turn.completed")])
    assert result["status"] == "passed"
