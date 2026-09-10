"""CLI behavior, including failures before cloud or filesystem changes."""

import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import modal
import pytest
from rich.console import Console

from modal_agents import cli, telemetry


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    telemetry.configure_telemetry()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.app, "result_action", "return_value")
    monkeypatch.delenv("OPENAI_AGENT_ID", raising=False)
    monkeypatch.delenv("OPENAI_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_EXECUTOR_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_CONTROLLER_API_KEY", raising=False)
    monkeypatch.setattr(cli, "console", Console(file=io.StringIO(), color_system=None))


@pytest.fixture
def cloud(monkeypatch):
    create = Mock()
    secret = Mock()
    lookup = Mock()
    run = Mock()
    monkeypatch.setattr(modal.Secret, "objects", Mock(create=create))
    monkeypatch.setattr(modal.Secret, "from_name", Mock(return_value=secret))
    monkeypatch.setattr(modal.App, "lookup", lookup)
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(
        modal.Function,
        "from_name",
        Mock(
            return_value=Mock(
                get_web_url=Mock(return_value="https://test.modal.run"),
                remote=Mock(return_value={"status": "ready"}),
            )
        ),
    )
    return create, secret, lookup, run


def initialize(*options):
    cli.app(
        [
            "init",
            "test-pool",
            "--api-key",
            "app-key",
            "--executor-key",
            "executor-key",
            "--agent-id",
            "agent_test",
            "--yes",
            *options,
        ]
    )


def test_init_generates_valid_files_and_stores_both_keys(cloud):
    create, _, _, run = cloud
    initialize("--no-deploy")
    assert create.call_args_list == [
        call(
            "openai-agents-test-pool-controller", {"OPENAI_API_KEY": "app-key"}, allow_existing=True
        ),
        call(
            "openai-agents-test-pool-executor",
            {"CODEX_API_KEY": "executor-key"},
            allow_existing=True,
        ),
        call(
            "openai-agents-test-pool-signing",
            {"OPENAI_WEBHOOK_SECRET": "pending-webhook-registration"},
            allow_existing=True,
        ),
    ]
    for path in (Path("agents/test-pool.py"), Path("hooks/test-pool_handler.py")):
        compile(path.read_text(), str(path), "exec")
    assert "agent_auto" not in Path("agents/test-pool.py").read_text()
    assert Path("agents/test-pool_executor.sh").stat().st_mode & 0o777 == 0o755
    run.assert_not_called()


def test_init_deploys_using_current_python(cloud):
    initialize()
    cloud[3].assert_called_once_with(
        [sys.executable, "-m", "modal", "deploy", "hooks/test-pool_handler.py"],
        check=True,
    )


@pytest.mark.parametrize("env_var", ["OPENAI_API_KEY", "OPENAI_EXECUTOR_API_KEY"])
def test_missing_key_fails_before_side_effects(cloud, monkeypatch, env_var):
    for key in {"OPENAI_API_KEY", "OPENAI_EXECUTOR_API_KEY"} - {env_var}:
        monkeypatch.setenv(key, "key")
    with pytest.raises(SystemExit, match=env_var):
        cli.app(["init", "test-pool", "--yes"])
    cloud[0].assert_not_called()
    assert not Path("agents").exists()


def test_init_reads_keys_from_environment(cloud, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-app")
    monkeypatch.setenv("OPENAI_EXECUTOR_API_KEY", "env-executor")
    cli.app(["init", "test-pool", "--yes", "--no-deploy", "--agent-id", "agent_test"])
    assert cloud[0].call_args_list[0].args[1] == {"OPENAI_API_KEY": "env-app"}
    assert cloud[0].call_args_list[1].args[1] == {"CODEX_API_KEY": "env-executor"}


def test_init_preserves_existing_files(cloud):
    path = Path("hooks/test-pool_handler.py")
    path.parent.mkdir()
    path.write_text("# User edits\n")
    with pytest.raises(SystemExit, match="File already exists"):
        initialize("--no-deploy")
    assert path.read_text() == "# User edits\n"
    cloud[0].assert_not_called()


def test_secret_creation_failure_is_not_suppressed(cloud):
    cloud[0].side_effect = modal.exception.AuthError("Invalid credentials")
    with pytest.raises(modal.exception.AuthError):
        initialize()
    assert not Path("agents").exists()
    cloud[3].assert_not_called()


def test_failed_deployment_does_not_report_success(cloud):
    cloud[3].side_effect = subprocess.CalledProcessError(1, "modal deploy")
    with pytest.raises(subprocess.CalledProcessError):
        initialize()
    assert "✓ Deployed" not in cli.console.file.getvalue()


def test_webhook_secret_merges_only_signing_key(cloud):
    initialize("--no-deploy")
    cloud[0].reset_mock()
    cli.app(["webhook-secret", "test-pool", "--secret", "whsec_dGVzdA==", "--no-deploy"])
    modal.Secret.from_name.assert_called_once_with("openai-agents-test-pool-signing")
    cloud[1].update.assert_called_once_with({"OPENAI_WEBHOOK_SECRET": "whsec_dGVzdA=="})
    cloud[0].assert_not_called()


@pytest.mark.parametrize("name", ["../outside", "a/b", ".", 'bad"name', "bad name"])
def test_invalid_pool_name_is_rejected_before_side_effects(cloud, name):
    with pytest.raises(SystemExit):
        cli.app(["init", name, "--yes"])
    cloud[0].assert_not_called()
    assert not Path("agents").exists()


def test_deploy_all_is_sorted_and_stops_on_failure(cloud):
    for name in ("z", "a", "m"):
        cli.app(
            [
                "init",
                name,
                "--api-key",
                "app",
                "--executor-key",
                "exec",
                "--agent-id",
                "agent_test",
                "--yes",
                "--no-deploy",
            ]
        )
    cloud[3].side_effect = [None, subprocess.CalledProcessError(1, "modal deploy")]
    with pytest.raises(subprocess.CalledProcessError):
        cli.app(["deploy"])
    assert cloud[3].call_args_list == [
        call(
            [sys.executable, "-m", "modal", "deploy", f"hooks/{name}_handler.py"],
            check=True,
        )
        for name in ("a", "m")
    ]


@pytest.mark.parametrize("args", [["deploy"], ["deploy", "missing"]])
def test_deploy_missing_handler_fails(cloud, args):
    with pytest.raises(SystemExit):
        cli.app(args)
    cloud[3].assert_not_called()


def test_list_pools_is_sorted():
    Path("agents").mkdir()
    for name in ("z", "a"):
        Path(f"agents/{name}.py").touch()
    Path("agents/a_executor.sh").touch()
    cli.app(["list"])
    assert cli.console.file.getvalue().splitlines() == ["a", "z"]


@pytest.mark.parametrize("deployed", [True, False])
def test_destroy_handles_deployed_and_local_pools(cloud, deployed):
    initialize("--no-deploy")
    if not deployed:
        cloud[2].side_effect = modal.exception.NotFoundError("Not deployed")
    cli.app(["destroy", "test-pool", "--yes"])
    assert not list(Path("agents").iterdir())
    assert not list(Path("hooks").iterdir())
    if deployed:
        cloud[3].assert_called_once_with(
            [
                sys.executable,
                "-m",
                "modal",
                "app",
                "stop",
                "openai-agents-test-pool",
                "--yes",
            ],
            check=True,
        )
    else:
        cloud[3].assert_not_called()


def test_destroy_preserves_files_when_stop_fails(cloud):
    initialize("--no-deploy")
    cloud[3].side_effect = subprocess.CalledProcessError(1, "modal app stop")
    with pytest.raises(subprocess.CalledProcessError):
        cli.app(["destroy", "test-pool", "--yes"])
    assert Path("hooks/test-pool_handler.py").exists()
    assert Path("agents/test-pool.py").exists()


def test_required_agent_id_and_distinct_keys(cloud):
    with pytest.raises(SystemExit, match="OPENAI_AGENT_ID"):
        cli.app(["init", "test-pool", "--api-key", "app", "--executor-key", "exec", "--yes"])
    with pytest.raises(SystemExit, match="separate"):
        cli.app(["init", "test-pool", "--api-key", "same", "--executor-key", "same", "--yes"])
    cloud[0].assert_not_called()


def test_doctor_local_has_no_cloud_calls(cloud):
    initialize("--no-deploy")
    cloud[0].reset_mock()
    cli.app(["doctor", "--local"])
    cloud[0].assert_not_called()
    cloud[2].assert_not_called()
    assert "local configuration is valid" in cli.console.file.getvalue()


def test_deploy_validates_whole_batch_before_deploying(cloud):
    initialize("--no-deploy")
    Path("hooks/z_handler.py").write_text("this is invalid python !")
    with pytest.raises(ValueError, match="Required file"):
        cli.app(["deploy"])
    cloud[3].assert_not_called()


def test_doctor_rejects_invalid_pool_type(cloud):
    initialize("--no-deploy")
    Path("agents/test-pool.py").write_text("pool = {}")
    with pytest.raises(ValueError, match="pool must be a Pool instance"):
        cli.app(["doctor", "--local"])


def test_doctor_cloud_checks_secret_names_and_endpoint(cloud, monkeypatch):
    initialize("--no-deploy")
    pool = cli._check_files("test-pool")
    # Mock.name is special; assign the public name attribute explicitly.
    secrets = []
    for name in pool.required_secrets:
        secret = Mock()
        secret.name = name
        secrets.append(secret)
    modal.Secret.objects.list.return_value = secrets
    endpoint = Mock(get_web_url=Mock(return_value="https://test.modal.run"))
    monkeypatch.setattr(modal.Function, "from_name", Mock(return_value=endpoint))
    cli.app(["doctor"])
    endpoint.get_web_url.assert_called_once()
    modal.Secret.objects.list.return_value = []
    with pytest.raises(SystemExit, match="Missing Modal secrets"):
        cli.app(["doctor"])


def test_doctor_missing_endpoint(cloud, monkeypatch):
    initialize("--no-deploy")
    pool = cli._check_files("test-pool")
    secrets = []
    for name in pool.required_secrets:
        secret = Mock()
        secret.name = name
        secrets.append(secret)
    modal.Secret.objects.list.return_value = secrets
    monkeypatch.setattr(
        modal.Function, "from_name", Mock(return_value=Mock(get_web_url=Mock(return_value=None)))
    )
    with pytest.raises(SystemExit, match="URL is missing"):
        cli.app(["doctor"])


def test_noninteractive_destroy_requires_yes(cloud, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="requires --yes"):
        cli.app(["destroy", "test-pool"])
    cloud[2].assert_not_called()


def test_destroy_cancelled_keeps_files(cloud, monkeypatch):
    initialize("--no-deploy")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.Confirm, "ask", lambda *args, **kwargs: False)
    cli.app(["destroy", "test-pool"])
    assert Path("agents/test-pool.py").exists()


def test_prompt_hides_credentials_and_rejects_empty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompt = Mock(return_value="secret")
    monkeypatch.setattr(cli.Prompt, "ask", prompt)
    assert cli._required_value(None, "OPENAI_API_KEY", yes=False) == "secret"
    prompt.assert_called_once_with("OPENAI_API_KEY", password=True)
    prompt.return_value = " "
    with pytest.raises(SystemExit, match="cannot be empty"):
        cli._required_value(None, "OPENAI_API_KEY", yes=False)


def test_run_redacts_sdk_errors(monkeypatch):
    monkeypatch.setattr(cli, "app", Mock(side_effect=modal.exception.AuthError("SECRET")))
    with pytest.raises(SystemExit) as error:
        cli.run()
    assert "SECRET" not in str(error.value)
    assert "AuthError" in str(error.value)


def test_run_reports_local_error(monkeypatch):
    monkeypatch.setattr(cli, "app", Mock(side_effect=ValueError("invalid config")))
    with pytest.raises(SystemExit, match="invalid config"):
        cli.run()


def test_init_can_store_separate_read_only_controller_key(cloud):
    initialize("--no-deploy", "--controller-key", "read-key")
    assert cloud[0].call_args_list[0].args[1] == {"OPENAI_API_KEY": "read-key"}
    output = cli.console.file.getvalue()
    assert "both OpenAI Platform API keys" in output
    assert "do not prove their scopes" in output
    assert "whsec_" in output


def test_controller_cannot_reuse_executor_key(cloud):
    with pytest.raises(SystemExit, match="separate"):
        initialize("--no-deploy", "--controller-key", "executor-key")
    cloud[0].assert_not_called()


def test_interactive_agent_selection_uses_selected_id(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "list_agents", Mock(return_value=[("agent_a", "A"), ("agent_b", "B")]))
    monkeypatch.setattr(cli.Prompt, "ask", Mock(return_value="2"))
    assert cli._select_agent("key", None, None, None, yes=False) == "agent_b"


def test_interactive_create_asks_for_name_and_model(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "list_agents", Mock(return_value=[]))
    monkeypatch.setattr(cli.Prompt, "ask", Mock(side_effect=["new", "Example", "chosen-model"]))
    create = Mock(return_value="agent_new")
    monkeypatch.setattr(cli, "create_agent", create)
    assert cli._select_agent("key", None, None, None, yes=False) == "agent_new"
    create.assert_called_once_with("key", "Example", "chosen-model")


def test_interactive_manual_agent_entry(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "list_agents", Mock(return_value=[]))
    monkeypatch.setattr(cli.Prompt, "ask", Mock(side_effect=["manual", "agent_manual"]))
    assert cli._select_agent("key", None, None, None, yes=False) == "agent_manual"


@pytest.mark.parametrize("name,model", [("", "model"), ("name", "")])
def test_interactive_creation_rejects_empty_settings(monkeypatch, name, model):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "list_agents", Mock(return_value=[]))
    monkeypatch.setattr(cli.Prompt, "ask", Mock(side_effect=["new", name, model]))
    with pytest.raises(SystemExit, match="cannot be empty"):
        cli._select_agent("key", None, None, None, yes=False)


def test_explicit_creation_requires_model_and_rejects_agent_id(monkeypatch):
    create = Mock(return_value="agent_new")
    monkeypatch.setattr(cli, "create_agent", create)
    with pytest.raises(SystemExit, match="requires --model"):
        cli._select_agent("key", None, "Name", None, yes=True)
    with pytest.raises(SystemExit, match="not both"):
        cli._select_agent("key", "agent_existing", "Name", "model", yes=True)
    create.assert_not_called()
    assert cli._select_agent("key", None, "Name", "model", yes=True) == "agent_new"


def test_webhook_secret_applies_and_checks_by_default(cloud, monkeypatch):
    initialize("--no-deploy")
    order = []
    monkeypatch.setattr(cli, "deploy", lambda name: order.append("deploy"))
    monkeypatch.setattr(cli, "doctor", lambda name, live: order.append(("doctor", live)))
    cli.app(["webhook-secret", "test-pool", "--secret", "whsec_dGVzdA=="])
    cloud[1].update.assert_called_once_with({"OPENAI_WEBHOOK_SECRET": "whsec_dGVzdA=="})
    assert order == ["deploy", ("doctor", True)]
    text = cli.console.file.getvalue()
    assert "https://test.modal.run" in text
    assert "SAME project" in text
    assert "agent.session.action_required" in text
    assert "agent.session.failed" in text


@pytest.mark.parametrize(
    "secret", ["pending-webhook-registration", "sk-not-a-signing-secret", "whsec_!!!"]
)
def test_webhook_secret_rejects_wrong_credential_before_mutation(cloud, secret):
    initialize("--no-deploy")
    with pytest.raises(SystemExit):
        cli.webhook_secret("test-pool", secret=secret, no_deploy=True)
    cloud[1].update.assert_not_called()


def test_guided_handoff_can_resume_later(cloud, monkeypatch):
    initialize("--no-deploy")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompt = Mock(return_value="")
    monkeypatch.setattr(cli.Prompt, "ask", prompt)
    cli._finish_setup("test-pool", "app", yes=False)
    assert prompt.call_args.kwargs["password"] is True
    assert "Setup pending" in cli.console.file.getvalue()
    cloud[1].update.assert_not_called()


def test_guided_handoff_applies_secret_and_offers_smoke(cloud, monkeypatch):
    initialize("--no-deploy")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.Prompt, "ask", Mock(return_value="whsec_dGVzdA=="))
    monkeypatch.setattr(cli.Confirm, "ask", Mock(return_value=True))
    update, smoke = Mock(), Mock()
    monkeypatch.setattr(cli, "webhook_secret", update)
    monkeypatch.setattr(cli, "smoke", smoke)
    cli._finish_setup("test-pool", "app", yes=False)
    update.assert_called_once_with("test-pool", secret="whsec_dGVzdA==")
    smoke.assert_called_once_with("test-pool", api_key="app")


def test_unattended_handoff_uses_env_but_does_not_run_smoke(cloud, monkeypatch):
    initialize("--no-deploy")
    monkeypatch.setenv("OPENAI_WEBHOOK_SECRET", "whsec_dGVzdA==")
    update, smoke = Mock(), Mock()
    monkeypatch.setattr(cli, "webhook_secret", update)
    monkeypatch.setattr(cli, "smoke", smoke)
    cli._finish_setup("test-pool", "app", yes=True)
    update.assert_called_once()
    smoke.assert_not_called()


def test_doctor_live_and_local_are_mutually_exclusive():
    with pytest.raises(SystemExit, match="not both"):
        cli.doctor(local=True, live=True)


def test_live_check_refuses_stale_or_missing_handler(cloud):
    initialize("--no-deploy")
    pool = cli._check_files("test-pool")
    modal.Function.from_name.return_value.remote.return_value = {
        "status": "not_ready",
        "detail": "old handler",
    }
    with pytest.raises(SystemExit, match=r"NOT READY.*old handler"):
        cli._live_check(pool, "https://test.modal.run")
    modal.Function.from_name.side_effect = modal.exception.NotFoundError("missing")
    with pytest.raises(SystemExit, match="redeploy"):
        cli._live_check(pool, "https://test.modal.run")


def test_live_check_success_is_not_called_e2e_success(cloud):
    initialize("--no-deploy")
    cli._live_check(cli._check_files("test-pool"), "https://test.modal.run")
    output = cli.console.file.getvalue()
    assert "live endpoint ready" in output
    assert "still require a live smoke test" in output
    assert "E2E passed" not in output


@pytest.mark.parametrize(
    "options",
    [{"timeout": 0}, {"timeout": 601}, {"prompt": " "}, {"api_key": "a", "api_key_secret": "b"}],
)
def test_smoke_rejects_invalid_options_before_cloud(cloud, options):
    with pytest.raises(SystemExit):
        cli.smoke("test-pool", **options)
    cloud[2].assert_not_called()


def test_smoke_prints_actual_response_after_readiness(cloud, monkeypatch):
    initialize("--no-deploy")
    doctor = Mock()
    run = Mock(
        return_value={"session_id": "sess_1", "sandbox_id": "sb_1", "response": "The real joke"}
    )
    monkeypatch.setattr(cli, "doctor", doctor)
    monkeypatch.setattr(cli, "run_smoke", run)
    cli.smoke("test-pool", api_key_secret="application-secret")
    doctor.assert_called_once_with("test-pool", live=True)
    assert run.call_args.kwargs["api_key_secret"] == "application-secret"
    assert "The real joke" in cli.console.file.getvalue()
    assert "cleanup confirmed" in cli.console.file.getvalue()


def test_full_interactive_wizard_runs_through_registration_and_smoke(cloud, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "list_agents", Mock(return_value=[]))
    create_agent = Mock(return_value="agent_new")
    monkeypatch.setattr(cli, "create_agent", create_agent)
    monkeypatch.setattr(
        cli.Prompt,
        "ask",
        Mock(
            side_effect=[
                "application-secret-value",
                "executor-secret-value",
                "new",
                "Wizard agent",
                "chosen-model",
                "whsec_dGVzdA==",
            ]
        ),
    )
    monkeypatch.setattr(cli.Confirm, "ask", Mock(return_value=True))

    def secrets():
        return [SimpleNamespace(name=call.args[0]) for call in cloud[0].call_args_list]

    modal.Secret.objects.list.side_effect = secrets
    run = Mock(
        return_value={
            "session_id": "sess_wizard",
            "sandbox_id": "sb_wizard",
            "response": "Wizard response",
        }
    )
    monkeypatch.setattr(cli, "run_smoke", run)
    cli.app(["init", "guided-pool"])
    create_agent.assert_called_once_with("application-secret-value", "Wizard agent", "chosen-model")
    assert len(cloud[3].call_args_list) == 2  # Initial deploy, then apply signing secret.
    assert "agent_new" in Path("agents/guided-pool.py").read_text()
    cloud[1].update.assert_called_once_with({"OPENAI_WEBHOOK_SECRET": "whsec_dGVzdA=="})
    assert (
        modal.Function.from_name.return_value.remote.call_count == 2
    )  # Readiness + smoke preflight.
    assert run.call_args.kwargs["api_key"] == "application-secret-value"
    output = cli.console.file.getvalue()
    assert "E2E passed" in output and "Wizard response" in output
    assert "application-secret-value" not in output and "executor-secret-value" not in output


def test_missing_preview_sdk_has_install_guidance(monkeypatch):
    def missing():
        raise ModuleNotFoundError("No module named agent_api_sdk", name="agent_api_sdk")

    monkeypatch.setattr(cli, "app", missing)
    with pytest.raises(SystemExit, match="Install it with:"):
        cli.run()


def test_unrelated_missing_dependency_is_not_masked(monkeypatch):
    def missing():
        raise ModuleNotFoundError("No module named other", name="other")

    monkeypatch.setattr(cli, "app", missing)
    with pytest.raises(ModuleNotFoundError, match="other"):
        cli.run()
