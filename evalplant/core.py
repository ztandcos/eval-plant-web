import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

STAGES = (
    "task_understanding",
    "repository_exploration",
    "fault_localization",
    "root_cause_analysis",
    "patch_implementation",
    "test_verification",
    "termination_decision",
    "non_agent_failure",
)

MECHANISMS = (
    "information_omission",
    "wrong_assumption",
    "tool_misuse",
    "invalid_edit",
    "feedback_ignored",
    "insufficient_verification",
    "budget_exhausted",
    "environment_or_benchmark_fault",
    "unknown",
)

VERDICTS = ("PASS", "FAIL", "TIMEOUT", "INFRA_ERROR", "UNKNOWN")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object in %s" % path)
    return value


def content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
        return "\n".join(parts)
    if value is None:
        return ""
    return str(value)


def extract_command(message: Dict[str, Any], text: str) -> Optional[str]:
    extra = message.get("extra") or {}
    for key in ("command", "action"):
        if isinstance(extra.get(key), str):
            return extra[key]

    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        function = tool_calls[0].get("function", {})
        arguments = function.get("arguments", "")
        try:
            arguments = json.loads(arguments)
        except (TypeError, json.JSONDecodeError):
            pass
        if isinstance(arguments, dict):
            return arguments.get("command") or arguments.get("cmd") or str(arguments)
        return str(arguments)

    match = re.search(
        r"```(?:bash|sh|shell)\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE
    )
    return match.group(1).strip() if match else None


def classify_step(role: str, text: str, command: Optional[str]) -> str:
    haystack = "%s\n%s" % (command or "", text[:1000])
    if role == "tool":
        return (
            "tool_error"
            if re.search(r"\b(error|exception|traceback)\b", haystack, re.I)
            else "tool_output"
        )
    if command:
        if re.search(r"\b(pytest|unittest|tox|nox)\b", command):
            return "test_execution"
        if re.search(r"\b(sed|cat|head|tail|rg|grep|find|ls)\b", command):
            return "file_read"
        if re.search(r"\b(apply_patch|patch)\b|(?:>|>>)\s*\S+", command):
            return "file_edit"
        return "shell_command"
    if role == "assistant":
        return "reasoning_summary"
    if role == "user":
        return "task"
    return role or "message"


def parse_test_result(text: str) -> str:
    if re.search(r"\b(0 failed|passed|OK)\b", text, re.I) and not re.search(
        r"\b[1-9]\d* failed\b", text, re.I
    ):
        return "passed"
    if re.search(r"\b(failed|failure|error|traceback)\b", text, re.I):
        return "failed"
    return "unknown"


def test_status(text: str, command: Optional[str]) -> Optional[str]:
    if not (command and re.search(r"\b(pytest|unittest|tox|nox)\b", command)):
        return None
    return parse_test_result(text)


def normalize_trajectory(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    messages = data.get("messages")
    if messages is None and isinstance(data.get("trajectory"), list):
        messages = data["trajectory"]
    if not isinstance(messages, list):
        raise ValueError("Trajectory must contain a messages or trajectory list")

    steps = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            message = {"role": "unknown", "content": message}
        role = str(message.get("role") or message.get("type") or "unknown")
        text = content_text(
            message.get("content")
            or message.get("response")
            or message.get("observation")
        )
        reasoning = content_text(message.get("reasoning_content"))
        if reasoning and reasoning not in text:
            text = (reasoning + "\n\n" + text).strip()
        command = extract_command(message, text)
        action_type = classify_step(role, text, command)
        if role == "tool" and steps and steps[-1]["action_type"] == "test_execution":
            action_type = "test_output"
            steps[-1]["test_status"] = parse_test_result(text)
        steps.append(
            {
                "step_index": index,
                "role": role,
                "action_type": action_type,
                "content": text,
                "command": command,
                "test_status": test_status(text, command),
            }
        )
    return steps


def summarize_step(step: Dict[str, Any], limit: int = 800) -> Dict[str, Any]:
    content = str(step.get("content") or "")
    return {
        "step": step["step_index"],
        "role": step.get("role"),
        "action": step.get("action_type"),
        "command": step.get("command"),
        "test_status": step.get("test_status"),
        "content": content[:limit],
    }


def signal_bundle(steps: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    steps = list(steps)
    commands = [step.get("command") for step in steps if step.get("command")]
    repeats = sorted({command for command in commands if commands.count(command) > 1})
    tests = [step for step in steps if step.get("action_type") == "test_execution"]
    errors = [
        step["step_index"] for step in steps if step.get("action_type") == "tool_error"
    ]
    terminal = [step for step in steps if step.get("role") == "exit"]
    return {
        "step_count": len(steps),
        "test_steps": [step["step_index"] for step in tests],
        "last_test_status": tests[-1].get("test_status") if tests else None,
        "repeated_commands": repeats[:10],
        "tool_error_steps": errors,
        "verification_missing": not tests,
        "terminal_steps": [step["step_index"] for step in terminal],
        "terminal_statuses": [step.get("content") for step in terminal],
    }


def validate_stage_mechanism(stage: str, mechanism: str) -> None:
    if stage not in STAGES:
        raise ValueError("Invalid stage: %s" % stage)
    if mechanism not in MECHANISMS:
        raise ValueError("Invalid mechanism: %s" % mechanism)
