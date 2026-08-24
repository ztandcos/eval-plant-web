import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

TAXONOMY = {
    "localization": {
        "issue_misleading": ("issue_misleading",),
        "superficial_information_matching": (
            "description_keywords",
            "referred_code",
            "error_stack_trace",
        ),
    },
    "repair": {
        "fix_strategy_defects": (
            "specific_case_overfitting",
            "evasive_repair",
            "redundant_erroneous_implementation",
        ),
        "implementation_detail_defects": (
            "algorithmic_implementation",
            "control_flow",
            "boundary_handling",
            "data_processing_errors",
            "insufficient_domain_knowledge",
        ),
        "incomplete_repair": (
            "inheritance_dependency",
            "interface_contract_dependency",
            "logic_coordination_dependency",
            "recurring_pattern_dependency",
            "issue_interference",
        ),
    },
    "iterative_verification": {
        "reproduction_or_verification_failure": (
            "reproduction_validation_run_failure",
            "insufficient_verification_capability",
            "reproduction_output_misreading",
        ),
        "iteration_anomalies": (
            "non_progressive_iteration",
            "blind_strategy_switching",
        ),
        "validation_retreat": ("verification_abandonment", "verification_weakening"),
        "context_amnesia": ("context_amnesia",),
    },
}

PHASES = tuple(TAXONOMY)
CATEGORIES = tuple(category for groups in TAXONOMY.values() for category in groups)
SUBCATEGORIES = tuple(
    subcategory
    for groups in TAXONOMY.values()
    for subcategories in groups.values()
    for subcategory in subcategories
)
# Compatibility names for the existing CLI and database columns.
STAGES = PHASES
MECHANISMS = CATEGORIES
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
    if isinstance(value, dict):
        nested = value.get("content") or value.get("text")
        return content_text(nested) if nested is not None else str(value)
    if isinstance(value, list):
        return "\n".join(content_text(item) for item in value if item is not None)
    return "" if value is None else str(value)


def _arguments(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"_raw": value}
        return parsed if isinstance(parsed, dict) else {"_raw": value}
    return {}


def extract_command(message: Dict[str, Any], text: str) -> Optional[str]:
    extra = message.get("extra") or {}
    for key in ("command", "action"):
        if isinstance(extra.get(key), str):
            return extra[key]
    calls = message.get("tool_calls") or []
    if calls:
        call = calls[0]
        function = call.get("function", call)
        arguments = _arguments(function.get("arguments"))
        return arguments.get("command") or arguments.get("cmd") or str(arguments)
    match = re.search(
        r"```(?:bash|sh|shell)\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE
    )
    return match.group(1).strip() if match else None


def classify_step(
    role: str, text: str, command: Optional[str], tool_name: Optional[str] = None
) -> str:
    haystack = "%s\n%s" % (command or "", text[:2000])
    if role == "tool":
        return_code = re.search(r'["\']returncode["\']\s*:\s*(-?\d+)', text)
        if return_code:
            return "tool_error" if int(return_code.group(1)) else "tool_output"
        return (
            "tool_error"
            if re.search(r"\b(error|exception|traceback|failed)\b", haystack, re.I)
            else "tool_output"
        )
    if tool_name and "str_replace" in tool_name:
        return "file_edit"
    if command:
        if re.search(
            r"\b(pytest|unittest|tox|nox)\b|"
            r"(?:^|&&|\|\||;)\s*(?:(?:bash|sh)\s+)?"
            r"(?:\./|[\w./-]+/)?[\w.-]*test[\w.-]*\.sh\b",
            command,
        ):
            return "test_execution"
        if re.search(r"\b(apply_patch|patch|sed\s+-i)\b", command) or re.search(
            r"(?<!\d)(?:>>|>)\s*(?!&|/dev/null)\S+", command
        ):
            return "file_edit"
        if re.search(r"\b(sed|cat|head|tail|rg|grep|find|ls)\b", command):
            return "file_read"
        return "shell_command"
    if role in ("agent", "assistant"):
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


def _normalize_atif(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    normalized = []
    for position, raw in enumerate(data.get("steps") or [], start=1):
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("source") or "unknown")
        calls = raw.get("tool_calls") or []
        call = calls[0] if calls and isinstance(calls[0], dict) else {}
        tool_name = str(call.get("function_name") or "") or None
        tool_arguments = _arguments(call.get("arguments"))
        command = tool_arguments.get("command") or tool_arguments.get("cmd")
        message = content_text(raw.get("message"))
        reasoning = content_text(raw.get("reasoning_content"))
        results = (raw.get("observation") or {}).get("results") or []
        observation = "\n".join(content_text(item.get("content")) for item in results)
        has_error = any(bool(item.get("extra", {}).get("error")) for item in results)
        text = "\n\n".join(part for part in (reasoning, message, observation) if part)
        action = classify_step(role, text, command, tool_name)
        if has_error:
            action = "tool_error"
        normalized.append(
            {
                "step_index": int(raw.get("step_id") or position),
                "role": role,
                "action_type": action,
                "content": text,
                "command": command,
                "tool_name": tool_name,
                "tool_arguments": tool_arguments or None,
                "test_status": (
                    parse_test_result(observation or text)
                    if action == "test_execution"
                    else None
                ),
            }
        )
    return normalized


def normalize_trajectory(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if str(data.get("schema_version") or "").startswith("ATIF-"):
        return _normalize_atif(data)
    messages = data.get("messages")
    if messages is None and isinstance(data.get("trajectory"), list):
        messages = data["trajectory"]
    if not isinstance(messages, list):
        raise ValueError("Trajectory must contain ATIF steps, messages, or trajectory")

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
                "tool_name": None,
                "tool_arguments": None,
                "test_status": (
                    parse_test_result(text) if action_type == "test_execution" else None
                ),
            }
        )
    return steps


def summarize_step(step: Dict[str, Any], limit: int = 12000) -> Dict[str, Any]:
    return {
        "step": step["step_index"],
        "role": step.get("role"),
        "action": step.get("action_type"),
        "tool": step.get("tool_name"),
        "arguments": step.get("tool_arguments"),
        "test_status": step.get("test_status"),
        "content": str(step.get("content") or "")[:limit],
    }


def signal_bundle(steps: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    steps = list(steps)
    call_steps: Dict[str, List[int]] = {}
    for step in steps:
        if step.get("tool_name") or step.get("command"):
            signature = json.dumps(
                [
                    step.get("tool_name"),
                    step.get("tool_arguments") or step.get("command"),
                ],
                sort_keys=True,
                ensure_ascii=False,
            )
            call_steps.setdefault(signature, []).append(step["step_index"])
    tests = [step for step in steps if step.get("action_type") == "test_execution"]
    errors = [step for step in steps if step.get("action_type") == "tool_error"]
    terminal = [step for step in steps if step.get("role") == "exit"]
    return {
        "step_count": len(steps),
        "tool_errors": [summarize_step(step, 2000) for step in errors],
        "duplicate_calls": [
            {"call": signature, "step_ids": ids}
            for signature, ids in call_steps.items()
            if len(ids) > 1
        ],
        "file_edit_steps": [
            step["step_index"]
            for step in steps
            if step.get("action_type") == "file_edit"
        ],
        "test_runs": [
            {"step_id": step["step_index"], "status": step.get("test_status")}
            for step in tests
        ],
        "verification_missing": not tests,
        "terminal_steps": [step["step_index"] for step in terminal],
        "terminal_statuses": [step.get("content") for step in terminal],
        "timeout_steps": [
            step["step_index"]
            for step in steps
            if re.search(
                r"timeout|time limit|limits? exceeded", str(step.get("content")), re.I
            )
        ],
    }


def validate_taxonomy(phase: str, category: str, subcategory: str) -> None:
    if phase not in TAXONOMY:
        raise ValueError("Invalid phase: %s" % phase)
    if category not in TAXONOMY[phase]:
        raise ValueError("Invalid category for %s: %s" % (phase, category))
    if subcategory not in TAXONOMY[phase][category]:
        raise ValueError("Invalid subcategory for %s: %s" % (category, subcategory))


def validate_stage_mechanism(stage: str, mechanism: str) -> None:
    if stage not in PHASES:
        raise ValueError("Invalid phase: %s" % stage)
    if mechanism not in TAXONOMY[stage]:
        raise ValueError("Invalid category for %s: %s" % (stage, mechanism))
