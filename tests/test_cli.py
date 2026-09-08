"""Tests for modal-agents CLI commands."""

from __future__ import annotations

import pytest

from modal_agents import cli




def test_pool_template_is_valid_python() -> None:
    """Test that pool template is valid Python when rendered."""
    from string import Template

    content = Template(cli.POOL_CONFIG_TEMPLATE).substitute(
        pool_name=repr("test-pool"),
        agent_id=repr("agent_abc"),
        secret_name=repr("my-secret"),
    )

    # Should compile without syntax errors
    compile(content, "<string>", "exec")
    assert "test-pool" in content
    assert "agent_abc" in content


def test_webhook_handler_template_imports_openai() -> None:
    """Test that webhook handler imports required dependencies."""
    assert "from openai import OpenAI" in cli.WEBHOOK_HANDLER_TEMPLATE
    assert "import modal" in cli.WEBHOOK_HANDLER_TEMPLATE
    assert "import httpx" in cli.WEBHOOK_HANDLER_TEMPLATE


def test_pool_has_executor_setup() -> None:
    """Test that pool template includes executor setup."""
    assert "npm install -g @openai/codex@alpha" in cli.POOL_CONFIG_TEMPLATE
    assert "modal.Image.debian_slim" in cli.POOL_CONFIG_TEMPLATE


def test_handler_webhook_signature_verification() -> None:
    """Test that handler verifies webhook signatures."""
    assert "OPENAI_WEBHOOK_SECRET" in cli.WEBHOOK_HANDLER_TEMPLATE
    assert "verify_webhook_signature" in cli.WEBHOOK_HANDLER_TEMPLATE


def test_executor_wrapper_runs_codex() -> None:
    """Test that executor wrapper runs codex exec-server."""
    assert "codex exec-server" in cli.EXECUTOR_WRAPPER_TEMPLATE
    assert "--environment-id" in cli.EXECUTOR_WRAPPER_TEMPLATE
    assert "OPENAI_EXECUTOR_API_KEY" in cli.EXECUTOR_WRAPPER_TEMPLATE


def test_handler_handles_environment_connection() -> None:
    """Test that handler responds to environment_connection events."""
    assert "environment_connection" in cli.WEBHOOK_HANDLER_TEMPLATE
    assert "handle_environment_connection" in cli.WEBHOOK_HANDLER_TEMPLATE


def test_handler_handles_session_failure() -> None:
    """Test that handler cleans up on session failure."""
    assert "agent.session.failed" in cli.WEBHOOK_HANDLER_TEMPLATE
    assert "handle_session_failed" in cli.WEBHOOK_HANDLER_TEMPLATE


def test_api_keys_separated() -> None:
    """Test that API key and executor key are kept separate."""
    # Handler should use both keys
    assert "OPENAI_API_KEY" in cli.WEBHOOK_HANDLER_TEMPLATE
    assert "OPENAI_EXECUTOR_API_KEY" in cli.WEBHOOK_HANDLER_TEMPLATE

    # Pool should reference secret but not use keys directly
    assert "OPENAI_SECRET_NAME" in cli.POOL_CONFIG_TEMPLATE


def test_pool_has_customization_comments() -> None:
    """Test that pool template has comments for customization."""
    # Should guide users to customize
    assert "#" in cli.POOL_CONFIG_TEMPLATE  # Has comments
    assert "Add" in cli.POOL_CONFIG_TEMPLATE or "add" in cli.POOL_CONFIG_TEMPLATE


def test_webhook_handler_async() -> None:
    """Test that webhook handler is async."""
    assert "async def" in cli.WEBHOOK_HANDLER_TEMPLATE or "await" in cli.WEBHOOK_HANDLER_TEMPLATE


def test_executor_wrapper_is_bash() -> None:
    """Test that executor wrapper is a bash script."""
    assert cli.EXECUTOR_WRAPPER_TEMPLATE.startswith("#!/usr/bin/env bash")
    assert "set -e" in cli.EXECUTOR_WRAPPER_TEMPLATE


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
