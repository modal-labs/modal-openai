"""Preview SDK contracts for selection and explicit creation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

agents_api = pytest.importorskip("modal_agents.agents_api", exc_type=ModuleNotFoundError)


@pytest.fixture
def api(monkeypatch):
    client = SimpleNamespace(agents=SimpleNamespace(list=AsyncMock(), create=AsyncMock()))
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr(agents_api, "AgentAPISDK", Mock(return_value=context))
    return client.agents


def test_list_uses_preview_page_field_and_paginates(api):
    api.list.side_effect = [
        SimpleNamespace(
            page=[SimpleNamespace(id="agent_a", name="A")], has_more=True, next_cursor="next"
        ),
        SimpleNamespace(page=[SimpleNamespace(id="agent_b", name=None)], has_more=False),
    ]
    assert agents_api.list_agents("key") == [("agent_a", "A"), ("agent_b", "agent_b")]
    assert api.list.call_args.kwargs == {"cursor": "next", "limit": 100}


def test_list_rejects_stuck_cursor(api):
    api.list.return_value = SimpleNamespace(page=[], has_more=True, next_cursor=None)
    with pytest.raises(ValueError, match="cursor"):
        agents_api.list_agents("key")


def test_create_preserves_explicit_model(api):
    api.create.return_value = SimpleNamespace(id="agent_created")
    assert agents_api.create_agent("key", "My agent", "chosen-model") == "agent_created"
    assert api.create.call_args.kwargs["model"] == "chosen-model"
    assert api.create.call_args.kwargs["name"] == "My agent"


def test_list_failure_is_not_treated_as_no_agents(api):
    api.list.side_effect = RuntimeError("API unavailable")
    with pytest.raises(RuntimeError, match="API unavailable"):
        agents_api.list_agents("key")
