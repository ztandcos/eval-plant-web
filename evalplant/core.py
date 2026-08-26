import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

HARNESS_LAYERS = {
    "H-E": "执行环境",
    "H-T": "工具链路",
    "H-C": "上下文管理",
    "H-L": "运行生命周期",
    "H-O": "可观测性",
    "H-V": "验证与判分",
    "H-G": "治理与限制",
}

LLM_CATEGORIES = {
    "L1": "目标理解与规划",
    "L2": "推理与决策",
    "L3": "行动与工具使用",
    "L4": "反馈、验证与结束",
}

DIAGNOSIS_STATUSES = (
    "ATTRIBUTED",
    "HARNESS_SUSPECTED",
    "UNDETERMINED",
    "INPUT_TOO_LARGE",
    "FAILED",
)
RESPONSIBILITIES = ("HARNESS", "LLM")
CONFIDENCE_LEVELS = ("HIGH", "MEDIUM", "LOW")
VERDICTS = ("PASS", "FAIL", "TIMEOUT", "INFRA_ERROR", "UNKNOWN", "INCOMPLETE")
SUPPORTED_ATIF_VERSIONS = {"ATIF-v1.%d" % minor for minor in range(8)}
CANONICAL_SCHEMA_VERSION = "evalplant-canonical-v1"
ADAPTER_VERSION = "atif-adapter-v1"

SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|password|secret)\b"
        r"(\s*[:=]\s*)[^\s,;\"']+"
    ),
)


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


def validate_trajectory_schema(data: Dict[str, Any]) -> Optional[str]:
    version = str(data.get("schema_version") or "")
    if not version:
        return None
    if version not in SUPPORTED_ATIF_VERSIONS:
        raise ValueError("Unsupported trajectory schema_version: %s" % version)
    if not isinstance(data.get("steps"), list):
        raise ValueError("ATIF trajectory must contain a steps array")
    for position, step in enumerate(data["steps"], start=1):
        if not isinstance(step, dict):
            raise ValueError("ATIF step %s must be an object" % position)
        if "step_id" not in step or "source" not in step:
            raise ValueError("ATIF step %s must contain step_id and source" % position)
    return version


def redact_text(text: str) -> str:
    redacted = text
    redacted = SECRET_PATTERNS[0].sub("<REDACTED_SECRET>", redacted)
    redacted = SECRET_PATTERNS[1].sub(r"\1<REDACTED_SECRET>", redacted)
    redacted = SECRET_PATTERNS[2].sub(r"\1\2<REDACTED_SECRET>", redacted)
    return redacted


def sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): (
                "<REDACTED_SECRET>"
                if re.search(
                    r"(?i)(api[_-]?key|access[_-]?token|password|secret)", str(key)
                )
                else sanitize_value(item)
            )
            for key, item in value.items()
        }
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
    version = validate_trajectory_schema(data)
    if version:
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


def summarize_step(
    step: Dict[str, Any], limit: Optional[int] = 12000
) -> Dict[str, Any]:
    content = str(step.get("content") or "")
    summary = {
        "step": step["step_index"],
        "role": step.get("role"),
        "action": step.get("action_type"),
        "tool": step.get("tool_name"),
        "arguments": step.get("tool_arguments"),
        "test_status": step.get("test_status"),
        "content": content if limit is None else content[:limit],
    }
    if limit is not None:
        summary["content_truncated"] = len(content) > limit
    return summary


def build_structured_index(steps: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    index = []
    for step in steps:
        content = str(step.get("content") or "")
        role = str(step.get("role") or "").lower()
        index.append(
            {
                "step_id": step["step_index"],
                "role": step.get("role"),
                "event_type": step.get("action_type"),
                "tool": step.get("tool_name"),
                "command": (step.get("command") or "")[:300] or None,
                "test_status": step.get("test_status"),
                "has_explicit_error": bool(
                    step.get("action_type") == "tool_error"
                    or step.get("test_status") == "failed"
                    or (
                        role in {"tool", "exit", "system"}
                        and re.search(
                            r"\b(error|exception|traceback|failed)\b",
                            content[:2000],
                            re.I,
                        )
                    )
                ),
            }
        )
    return index


def structured_failure_facts(
    steps: Iterable[Dict[str, Any]], runtime: Dict[str, Any], verifier_log: str
) -> Dict[str, Any]:
    steps = list(steps)
    index = build_structured_index(steps)
    anomalies = [item["step_id"] for item in index if item["has_explicit_error"]]
    failed_tests = [
        item["step_id"] for item in index if item.get("test_status") == "failed"
    ]
    first_anomaly = min(anomalies + failed_tests) if anomalies or failed_tests else None
    earlier = [
        item["step_id"]
        for item in index
        if first_anomaly is not None and item["step_id"] < first_anomaly
    ]
    last_agent = next(
        (
            step
            for step in reversed(steps)
            if step.get("role") in ("agent", "assistant")
        ),
        None,
    )
    last_agent_text = str((last_agent or {}).get("content") or "")
    completion_claim = bool(
        re.search(
            r"\b(done|completed|finished|successful)\b|任务已完成|修复完成",
            last_agent_text,
            re.I,
        )
        and not re.search(
            r"\b(not|failed|unable)\b|未完成|失败|无法", last_agent_text, re.I
        )
    )
    verifier_failed = runtime.get("verdict") == "FAIL" or bool(
        re.search(r"\b(failed|failure|error)\b", verifier_log, re.I)
    )
    return {
        "step_count": len(steps),
        # This is only a navigation boundary; absence of an error is not proof
        # that the step succeeded.
        "last_pre_anomaly_step": max(earlier) if earlier else None,
        "first_anomaly_step": first_anomaly,
        "tool_error_steps": [
            item["step_id"] for item in index if item["event_type"] == "tool_error"
        ],
        "failed_test_steps": failed_tests,
        "file_edit_steps": [
            item["step_id"] for item in index if item["event_type"] == "file_edit"
        ],
        "missing_tool_results": runtime.get("missing_tool_results") or [],
        "context_truncated": bool(runtime.get("context_truncation_markers")),
        "verifier_status": "FAIL" if verifier_failed else runtime.get("verdict"),
        "termination_reason": runtime.get("exception_type") or "recorded_end",
        "agent_verifier_conflict": bool(completion_claim and verifier_failed),
    }


def retrieval_step_ids(
    steps: Iterable[Dict[str, Any]],
    facts: Dict[str, Any],
    requested: Iterable[int] = (),
) -> List[int]:
    steps = list(steps)
    positions = {step["step_index"]: position for position, step in enumerate(steps)}
    requested_ids = {int(item) for item in requested if int(item) in positions}
    if requested_ids:
        return sorted(requested_ids)[:100]
    seeds = set()
    for key in ("first_anomaly_step", "last_pre_anomaly_step"):
        if facts.get(key) in positions:
            seeds.add(int(facts[key]))
    for key in ("tool_error_steps", "failed_test_steps"):
        items = [int(item) for item in facts.get(key, []) if int(item) in positions]
        seeds.update(items[:10] + items[-10:])
    seeds.update(step["step_index"] for step in steps[:2] + steps[-3:])
    selected = set()
    for step_id in seeds:
        position = positions[step_id]
        for neighbor in steps[max(0, position - 2) : position + 3]:
            selected.add(neighbor["step_index"])
    return sorted(selected)[:80]


def build_segment_index(
    index: List[Dict[str, Any]], max_segments: int = 100
) -> List[Dict[str, Any]]:
    if not index:
        return []
    segment_size = max(1, math.ceil(len(index) / max_segments))
    segments = []
    for offset in range(0, len(index), segment_size):
        chunk = index[offset : offset + segment_size]
        event_counts: Dict[str, int] = {}
        for item in chunk:
            event = str(item.get("event_type") or "unknown")
            event_counts[event] = event_counts.get(event, 0) + 1
        notable = [
            item["step_id"]
            for item in chunk
            if item.get("has_explicit_error")
            or item.get("test_status") in {"passed", "failed"}
            or item.get("event_type") == "file_edit"
        ]
        segments.append(
            {
                "start_step": chunk[0]["step_id"],
                "end_step": chunk[-1]["step_id"],
                "step_count": len(chunk),
                "event_counts": event_counts,
                "notable_step_ids": notable[:20],
                "notable_steps_omitted": max(0, len(notable) - 20),
            }
        )
    return segments


def hierarchical_trajectory_view(
    steps: Iterable[Dict[str, Any]],
    facts: Dict[str, Any],
    requested: Iterable[int] = (),
) -> Dict[str, Any]:
    steps = list(steps)
    selected = set(retrieval_step_ids(steps, facts, requested))
    index = build_structured_index(steps)
    return {
        "mode": "HIERARCHICAL",
        "total_steps": len(steps),
        "indexed_steps": len(steps),
        "segment_index": build_segment_index(index),
        "retrieved_steps": [
            summarize_step(step, 1200)
            for step in steps
            if step["step_index"] in selected
        ],
        "retrieved_step_ids": sorted(selected),
        "coverage": {
            "all_steps_indexed": True,
            "raw_trace_available": True,
            "raw_steps_included": len(selected),
        },
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


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    # Conservative without tying the platform to one model-specific tokenizer.
    return max(1, (len(text) + 1) // 2)


def validate_category(responsibility: str, category_code: str) -> str:
    if responsibility == "HARNESS" and category_code in HARNESS_LAYERS:
        return HARNESS_LAYERS[category_code]
    if responsibility == "LLM" and category_code in LLM_CATEGORIES:
        return LLM_CATEGORIES[category_code]
    raise ValueError("Invalid %s category: %s" % (responsibility, category_code))
