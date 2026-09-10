"""Exercise generated deployment entry points against the real Modal SDK."""

import os
import subprocess
from pathlib import Path
from runpy import run_path
from string import Template

import modal

from modal_agents.templates import EXECUTOR_WRAPPER, POOL_CONFIG, WEBHOOK_HANDLER


def test_generated_handler_registers_real_modal_functions(tmp_path, monkeypatch):
    root = tmp_path / "configuration"
    (root / "agents").mkdir(parents=True)
    (root / "hooks").mkdir()
    values = {"pool_name": repr("test-pool"), "agent_id": repr("agent_test")}
    (root / "agents/test-pool.py").write_text(Template(POOL_CONFIG).substitute(values))
    (root / "agents/test-pool_executor.sh").write_text(EXECUTOR_WRAPPER)
    handler = root / "hooks/test-pool_handler.py"
    handler.write_text(Template(WEBHOOK_HANDLER).substitute(values))
    # Record real decorator results, so SDK scope/serialization validation still runs.
    original = modal.App.function
    functions = {}

    def register(self, **options):
        decorate = original(self, **options)

        def wrapped(function):
            result = decorate(function)
            functions[options["name"]] = result
            return result

        return wrapped

    monkeypatch.setattr(modal.App, "function", register)
    monkeypatch.chdir(tmp_path)  # Paths resolve from the handler, independent of cwd.
    namespace = run_path(str(handler))
    assert namespace["app"].name == "openai-agents-test-pool"
    assert set(functions) == {"webhook", "reconcile", "check_webhook"}
    assert all(isinstance(function, modal.Function) for function in functions.values())


def test_executor_passes_environment_as_one_argument_and_uses_workspace(tmp_path: Path):
    script = tmp_path / "executor.sh"
    script.write_text(EXECUTOR_WRAPPER)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_codex = bin_dir / "codex"
    fake_codex.write_text('#!/bin/sh\npwd\nprintf "%s\\n" "$@"\n')
    fake_codex.chmod(0o755)
    workspace = tmp_path / "workspace with spaces"
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CODEX_API_KEY": "restricted",
        "MODAL_AGENTS_WORKSPACE": str(workspace),
    }
    environment_id = "environment with spaces; echo should-not-execute"
    result = subprocess.run(
        ["bash", str(script), environment_id], env=env, check=True, capture_output=True, text=True
    )
    assert result.stdout.splitlines() == [
        str(workspace),
        "exec-server",
        "--remote",
        "https://api.openai.com/v1/agents/api",
        "--environment-id",
        environment_id,
    ]
    env.pop("CODEX_API_KEY")
    result = subprocess.run(
        ["bash", str(script), "env"], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "CODEX_API_KEY" in result.stderr
