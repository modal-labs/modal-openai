"""Validate configuration before any cloud side effects."""

from pathlib import Path
from string import Template

import modal
import pytest
from pydantic import ValidationError

from modal_agents.pool import Pool, load_pool
from modal_agents.templates import POOL_CONFIG


def test_generated_pool_loads_and_separates_secrets(tmp_path):
    path = tmp_path / "test-pool.py"
    path.write_text(
        Template(POOL_CONFIG).substitute(pool_name=repr("test-pool"), agent_id=repr("agent_test"))
    )
    pool = load_pool(path)
    assert pool.name == "test-pool"
    assert pool.agent_id == "agent_test"
    assert pool.required_secrets == (
        "openai-agents-test-pool-controller",
        "openai-agents-test-pool-executor",
        "openai-agents-test-pool-signing",
    )


@pytest.mark.parametrize(
    "options",
    [
        {"name": "../outside"},
        {"name": "a" * 41},
        {"agent_id": "agent_auto"},
        {"agent_id": ""},
        {"cpu": 0},
        {"memory": -1},
        {"memory": True},
        {"timeout": 0},
        {"timeout": 86401},
        {"workspace": "relative"},
        {"workspace": "/workspace/../etc"},
        {"workspace": "/workspace/"},
        {"worker_secret_names": ("openai-agents-other-controller",)},
        {"worker_secret_names": ("",)},
        {"worker_secret_names": ("same", "same")},
        {"typo": True},
    ],
)
def test_invalid_pool_is_rejected(options):
    values = {"name": "test-pool", "agent_id": "agent_test", "image": modal.Image.debian_slim()}
    with pytest.raises(ValidationError):
        Pool(**(values | options))


def test_user_resource_configuration():
    pool = Pool(
        name="gpu",
        agent_id="agent_test",
        image=modal.Image.debian_slim(),
        gpu="A10G",
        cpu=4,
        memory=8192,
        workspace="/work",
        worker_secret_names=("data",),
    )
    assert pool.gpu == "A10G"
    assert pool.workspace == "/work"
    assert pool.required_secrets[-1] == "data"


@pytest.mark.parametrize(
    "content,match",
    [
        ("pool = {}", "pool must be a Pool instance"),
        (
            "from modal_agents.pool import Pool\nimport modal\n"
            "pool = Pool(name='other', agent_id='agent_test', image=modal.Image.debian_slim())",
            "filename",
        ),
    ],
)
def test_load_rejects_invalid_and_mismatched_config(tmp_path: Path, content, match):
    path = tmp_path / "test-pool.py"
    path.write_text(content)
    with pytest.raises(ValueError, match=match):
        load_pool(path)
