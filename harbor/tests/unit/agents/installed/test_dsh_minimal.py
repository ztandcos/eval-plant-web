"""Tests for the official DeepSeek Harness minimal SDK adapter."""

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from harbor.agents.factory import AgentFactory
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.agents.installed.dsh_minimal import DshMinimal
from harbor.agents.installed.dsh_minimal_runner import events_to_trajectory
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext
from harbor.models.agent.name import AgentName
from harbor.models.trajectories import Trajectory


def _agent(tmp_path, **kwargs) -> DshMinimal:
    kwargs.setdefault("model_name", "deepseek/deepseek-v4-flash")
    kwargs.setdefault("extra_env", {"DEEPSEEK_API_KEY": "test-key"})
    return DshMinimal(logs_dir=tmp_path, **kwargs)


def test_dsh_minimal_is_registered() -> None:
    assert AgentFactory.get_agent_class(AgentName.DSH_MINIMAL) is DshMinimal
    assert DshMinimal.name() == "dsh-minimal"


def test_dsh_minimal_pins_reproducible_sdk_by_default(tmp_path) -> None:
    assert _agent(tmp_path).version() == "0.1.0rc7"


def test_official_composition_rejects_skills(tmp_path) -> None:
    with pytest.raises(ValueError, match="Skills"):
        _agent(tmp_path, skills_dir="/skills")


@pytest.mark.asyncio
async def test_matching_preinstalled_sdk_skips_heavy_install(tmp_path) -> None:
    agent = _agent(tmp_path, version="0.1.0-rc.7")
    environment = AsyncMock()
    environment.exec.return_value = ExecResult(
        return_code=0, stdout="0.1.0rc7\n", stderr=""
    )
    ensure_dependencies = AsyncMock()
    exec_as_root = AsyncMock()
    exec_as_agent = AsyncMock()
    agent.ensure_system_dependencies = cast(Any, ensure_dependencies)
    agent.exec_as_root = cast(Any, exec_as_root)
    agent.exec_as_agent = cast(Any, exec_as_agent)

    await agent.install(environment)

    ensure_dependencies.assert_not_awaited()
    exec_as_agent.assert_not_awaited()
    assert environment.upload_file.await_count == 2
    assert exec_as_root.await_count == 2


@pytest.mark.asyncio
async def test_mismatched_sdk_installs_requested_version(tmp_path) -> None:
    agent = _agent(tmp_path, version="0.1.0-rc.7")
    environment = AsyncMock()
    environment.default_user = "agent"
    environment.exec.return_value = ExecResult(
        return_code=0, stdout="0.1.0rc5\n", stderr=""
    )
    exec_as_root = AsyncMock()
    exec_as_agent = AsyncMock()
    agent.ensure_system_dependencies = cast(Any, AsyncMock())
    agent.exec_as_root = cast(Any, exec_as_root)
    agent.exec_as_agent = cast(Any, exec_as_agent)

    await agent.install(environment)

    exec_as_agent.assert_awaited_once()
    if exec_as_agent.await_args is None:
        raise AssertionError("SDK installation was not awaited")
    command = exec_as_agent.await_args.kwargs["command"]
    assert "uv python install 3.12" in command
    assert "deepseek-harness-sdk==0.1.0-rc.7" in command
    assert exec_as_root.await_count == 3


@pytest.mark.asyncio
async def test_apt_mirror_failure_is_retried(tmp_path, monkeypatch) -> None:
    agent = _agent(tmp_path)
    agent._APT_INSTALL_RETRY_DELAY_SEC = 0
    calls = 0

    async def flaky_install(self_, environment_, dependencies_) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise NonZeroAgentExitCodeError("File has unexpected size")

    monkeypatch.setattr(
        "harbor.agents.installed.base.BaseInstalledAgent.ensure_system_dependencies",
        flaky_install,
    )

    await agent.ensure_system_dependencies(AsyncMock(), ("curl",))

    assert calls == 2


@pytest.mark.asyncio
async def test_run_invokes_sdk_runner_with_harbor_paths(tmp_path) -> None:
    agent = _agent(tmp_path)
    environment = AsyncMock()
    environment.default_user = "root"
    uploaded_env = ""

    async def capture_env(source, destination) -> None:
        nonlocal uploaded_env
        uploaded_env = source.read_text()

    environment.upload_file.side_effect = capture_env
    exec_as_agent = AsyncMock()
    agent.exec_as_agent = cast(Any, exec_as_agent)
    exec_as_root = AsyncMock()
    agent.exec_as_root = cast(Any, exec_as_root)

    await agent.run("fix the tests", environment, AgentContext())

    call = exec_as_agent.await_args
    if call is None:
        raise AssertionError("SDK runner was not awaited")
    command = call.kwargs["command"]
    assert "dsh_minimal_runner.py" in command
    assert ". /installed-agent/dsh_minimal.env" in command
    assert "test-key" not in command
    assert "env" not in call.kwargs
    assert "export DSH_MODEL=deepseek-v4-flash" in uploaded_env
    assert "export DSH_SESSION_ROOT=/logs/agent/dsh-sessions" in uploaded_env
    assert "export DEEPSEEK_API_KEY=test-key" in uploaded_env
    exec_as_root.assert_awaited_once()


@pytest.mark.asyncio
async def test_official_minimal_rejects_non_deepseek_routes(tmp_path) -> None:
    agent = DshMinimal(
        logs_dir=tmp_path,
        model_name="openai/gpt-5",
        extra_env={"OPENAI_API_KEY": "test-key"},
    )

    with pytest.raises(ValueError, match="supports DeepSeek models only"):
        await agent.run("fix the tests", AsyncMock(), AgentContext())


def test_sdk_events_are_exported_as_valid_atif() -> None:
    events = [
        {"type": "request/header", "data": {"model": "deepseek-v4-flash"}},
        {
            "type": "assistant/message",
            "data": {
                "message": {
                    "content": [
                        {"type": "reasoning", "text": "Inspect first."},
                        {
                            "type": "tool-call",
                            "id": "call-1",
                            "name": "bash",
                            "arguments": '{"command":"pwd"}',
                        },
                    ]
                },
                "usage": {
                    "inputTokens": 10,
                    "cacheReadTokens": 2,
                    "cacheWriteTokens": 1,
                    "outputTokens": 3,
                },
            },
        },
        {
            "type": "tool/result",
            "data": {
                "message": {
                    "content": [
                        {
                            "toolCallId": "call-1",
                            "content": [{"type": "text", "text": "/task"}],
                        }
                    ]
                }
            },
        },
        {
            "type": "assistant/message",
            "data": {
                "message": {"content": [{"type": "text", "text": "Done"}]},
                "usage": {"inputTokens": 20, "outputTokens": 4},
            },
        },
    ]

    trajectory = Trajectory.model_validate(
        events_to_trajectory(
            "fix the tests", "session-1", events, "deepseek-v4-flash", "0.1.0rc7"
        )
    )

    assert trajectory.steps[1].tool_calls is not None
    assert trajectory.steps[1].observation is not None
    assert trajectory.steps[1].observation.results[0].content == "/task"
    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_prompt_tokens == 33
    assert trajectory.final_metrics.total_completion_tokens == 7
