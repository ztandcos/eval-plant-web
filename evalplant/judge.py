import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core import (
    ADAPTER_VERSION,
    CANONICAL_SCHEMA_VERSION,
    CONFIDENCE_LEVELS,
    HARNESS_LAYERS,
    LLM_CATEGORIES,
    estimate_tokens,
    hierarchical_trajectory_view,
    normalize_trajectory,
    read_json,
    sanitize_value,
    signal_bundle,
    structured_failure_facts,
    summarize_step,
    validate_category,
    validate_trajectory_schema,
)

PROMPT_PATH = Path(__file__).with_name("diagnosis_prompt.txt")
PROMPT_VERSION = "engineering_diagnosis_v3"
RULE_VERSION = "harness_rules_v2"
DEFAULT_MAX_INPUT_TOKENS = 100_000
DEFAULT_MAX_OUTPUT_TOKENS = int(os.getenv("EVALPLANT_JUDGE_MAX_OUTPUT_TOKENS", "4096"))
THINKING_CONFIG = os.getenv("EVALPLANT_JUDGE_THINKING", "disabled")
REASONING_EFFORT = os.getenv("EVALPLANT_JUDGE_REASONING_EFFORT", "high")
THINKING_LABEL = (
    "%s:%s" % (THINKING_CONFIG, REASONING_EFFORT)
    if THINKING_CONFIG == "enabled"
    else THINKING_CONFIG
)
TEMPERATURE = 0


def diagnosis_config_hash(model: str, max_input_tokens: int) -> str:
    payload = {
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "rule_version": RULE_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "thinking": THINKING_LABEL,
        "temperature": TEMPERATURE,
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _result_data(raw_path: Path) -> Tuple[Dict[str, Any], Optional[Path]]:
    result_path = raw_path.parent.parent / "result.json"
    return (read_json(result_path), result_path) if result_path.exists() else ({}, None)


def runtime_facts(
    raw_path: Path,
    verdict: str,
    health_status: str,
    final_log: str,
    steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    trajectory = read_json(raw_path)
    source_schema_version = validate_trajectory_schema(trajectory)
    result, result_path = _result_data(raw_path)
    exception = result.get("exception_info") or {}
    calls = {}
    results = set()
    for step in trajectory.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for call in step.get("tool_calls") or []:
            call_id = str(call.get("tool_call_id") or "")
            if call_id:
                calls[call_id] = {
                    "step_id": step.get("step_id"),
                    "tool": call.get("function_name"),
                }
        for item in (step.get("observation") or {}).get("results") or []:
            source_id = str(item.get("source_call_id") or "")
            if source_id:
                results.add(source_id)
    missing = [
        {"tool_call_id": key, **value}
        for key, value in calls.items()
        if key not in results
    ]
    truncation_markers = []
    containers = [trajectory.get("extra") or {}, result.get("extra") or {}]
    for step in trajectory.get("steps") or []:
        if isinstance(step, dict):
            containers.append(step.get("extra") or {})
            for item in (step.get("observation") or {}).get("results") or []:
                if isinstance(item, dict):
                    containers.append(item.get("extra") or {})
    for item in containers:
        if not isinstance(item, dict):
            continue
        for key in ("truncated", "context_truncated", "is_truncated"):
            if item.get(key) is True:
                truncation_markers.append("%s=true" % key)
    return {
        "verdict": verdict,
        "health_status": health_status,
        "trajectory_valid": bool(steps),
        "trajectory_path": str(raw_path),
        "source_schema_version": source_schema_version or "legacy",
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "result_path": str(result_path) if result_path else None,
        "verifier_present": bool(result.get("verifier_result")),
        "exception_type": str(exception.get("exception_type") or ""),
        "exception_message": str(
            exception.get("exception_message") or exception.get("message") or ""
        ),
        "missing_tool_results": missing,
        "context_truncation_markers": truncation_markers[:3],
        "signals": signal_bundle(steps),
    }


def _rule_evidence(source: str, quote: str, explanation: str) -> List[Dict[str, Any]]:
    return [
        {"source": source, "step_id": None, "quote": quote, "explanation": explanation}
    ]


def _rule_report(
    code: str,
    component: str,
    matched_rule: str,
    summary: str,
    evidence: List[Dict[str, Any]],
    facts: Dict[str, Any],
) -> Dict[str, Any]:
    primary = {
        "responsibility": "HARNESS",
        "category_code": code,
        "step_id": None,
        "component": component,
        "contract_violation": matched_rule,
        "claim": summary,
    }
    return {
        "status": "ATTRIBUTED",
        "responsibility": "HARNESS",
        "category_code": code,
        "category_name": HARNESS_LAYERS[code],
        "root_cause_step": None,
        "component": component,
        "summary": summary,
        "primary_cause": primary,
        "secondary_factors": [],
        "failure_surface": None,
        "evidence": evidence,
        "causal_chain": [{"step_id": None, "role": "TRIGGER", "claim": summary}],
        "counterfactual": {
            "intervention": "修复 %s 契约违反" % matched_rule,
            "expected_effect": "该 Harness 故障不会继续阻断任务。",
            "strength": "STRONG",
        },
        "rejected_candidates": [],
        "contributing_factor": None,
        "confidence": "HIGH",
        "decision_source": "RULE",
        "matched_rule": matched_rule,
        "judge_model": None,
        "prompt_version": PROMPT_VERSION,
        "rule_version": RULE_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "judge_input_tokens": 0,
        "judge_output_tokens": 0,
        "judge_latency_seconds": 0.0,
        "judge_thinking": "not_called",
        "judge_temperature": TEMPERATURE,
        "judge_call_count": 0,
        "trajectory_mode": "RULE",
        "evidence_validation_level": "PROVENANCE_AND_CONTRACT",
        "max_output_tokens": 0,
        "runtime_facts": facts,
    }


def match_harness_rule(facts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    exception = "%s: %s" % (facts["exception_type"], facts["exception_message"])
    lowered = exception.lower()
    source = facts.get("result_path") or "database"

    if facts["missing_tool_results"]:
        first = facts["missing_tool_results"][0]
        quote = json.dumps(first, ensure_ascii=False)
        return _rule_report(
            "H-T",
            str(first.get("tool") or "tool_adapter"),
            "missing_tool_result",
            "工具调用已发出，但没有对应的工具返回。",
            _rule_evidence("trajectory", quote, "调用 ID 没有匹配的 observation。"),
            facts,
        )

    if facts["context_truncation_markers"]:
        quote = facts["context_truncation_markers"][0]
        return _rule_report(
            "H-C",
            "context_builder",
            "context_truncation_recorded",
            "运行记录明确显示上下文被截断。",
            _rule_evidence("trajectory_or_log", quote, "存在明确截断标记。"),
            facts,
        )

    if facts["health_status"] == "INCOMPLETE":
        return _rule_report(
            "H-L",
            "run_lifecycle",
            "run_incomplete",
            "任务运行没有完成生命周期，也没有产生完整结果。",
            _rule_evidence(
                "database", "health_status=INCOMPLETE", "导入记录标记运行不完整。"
            ),
            facts,
        )

    if facts["verdict"] == "UNKNOWN" and not facts["verifier_present"]:
        return _rule_report(
            "H-V",
            "verifier",
            "verifier_missing",
            "任务没有可用的 Verifier 结果，无法判断是否完成。",
            _rule_evidence(
                "result.json", "verifier_result missing", "结果文件缺少 Verifier 结论。"
            ),
            facts,
        )

    # A normal agent timeout can be caused by the model looping. Only explicit
    # infrastructure failures are classified from exception text here.
    if facts["health_status"] != "INFRA_ERROR":
        return None

    if re.search(
        r"docker|compose|address pools|subnetted|apt-get|apt update|"
        r"failed to create network|image .*building",
        lowered,
    ):
        code, component, rule = "H-E", "execution_runtime", "execution_exception"
    elif re.search(r"verifier|grader|reward|test[-_ ]?runner", lowered):
        code, component, rule = "H-V", "verifier", "verifier_exception"
    elif re.search(r"tool|function|mcp|schema|argument", lowered):
        code, component, rule = "H-T", "tool_adapter", "tooling_exception"
    elif re.search(
        r"context|prompt|history|memory|token.{0,20}(?:limit|length)|truncat",
        lowered,
    ):
        code, component, rule = "H-C", "context_builder", "context_exception"
    elif re.search(r"trace|trajectory|telemetry|log|seriali|deseriali", lowered):
        code, component, rule = "H-O", "observability", "observability_exception"
    elif re.search(r"scheduler|orchestrat|retry|state|lifecycle|cancel", lowered):
        code, component, rule = "H-L", "orchestrator", "lifecycle_exception"
    elif re.search(r"guardrail|policy|budget|not allowed|safety", lowered):
        code, component, rule = "H-G", "governance", "governance_exception"
    else:
        code, component, rule = "H-E", "execution_runtime", "execution_exception"
    quote = exception.strip(": ") or "health_status=INFRA_ERROR"
    return _rule_report(
        code,
        component,
        rule,
        "运行基础设施发生明确异常：%s" % quote,
        _rule_evidence(source, quote, "该异常足以中断任务运行。"),
        facts,
    )


def _json_call(
    client: Any, model: str, system: str, payload: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    started = time.monotonic()
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "extra_body": {"thinking": {"type": THINKING_CONFIG}},
    }
    if THINKING_CONFIG == "enabled":
        request["reasoning_effort"] = REASONING_EFFORT
    else:
        request["temperature"] = TEMPERATURE
    response = client.chat.completions.create(**request)
    content = response.choices[0].message.content
    if not content:
        raise ValueError("Judge returned an empty response")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("Judge response must be a JSON object")
    usage = getattr(response, "usage", None)
    return result, {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "latency_seconds": time.monotonic() - started,
    }


def _quote_present(quote: str, source: str) -> bool:
    if quote in source:
        return True
    normalized_quote = " ".join(quote.split())
    normalized_source = " ".join(source.split())
    return bool(normalized_quote and normalized_quote in normalized_source)


def _string_values(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _string_values(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _string_values(nested)]
    return []


def _validate_llm_report(
    result: Dict[str, Any],
    steps: List[Dict[str, Any]],
    final_log: str,
    facts: Dict[str, Any],
    model: str,
    usage: Dict[str, Any],
    trajectory_mode: str,
    config_hash: str,
) -> Dict[str, Any]:
    status = str(result.get("status") or "").upper()
    common = {
        "decision_source": "LLM",
        "matched_rule": None,
        "judge_model": model,
        "prompt_version": PROMPT_VERSION,
        "rule_version": RULE_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "diagnosis_config_hash": config_hash,
        "judge_input_tokens": usage["input_tokens"],
        "judge_output_tokens": usage["output_tokens"],
        "judge_latency_seconds": usage["latency_seconds"],
        "judge_thinking": THINKING_LABEL,
        "judge_temperature": TEMPERATURE,
        "judge_call_count": int(usage.get("call_count") or 1),
        "trajectory_mode": trajectory_mode,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "runtime_facts": facts,
    }
    if status in ("UNDETERMINED", "HARNESS_SUSPECTED"):
        suspected = str(result.get("suspected_layer") or "") or None
        if suspected and suspected not in HARNESS_LAYERS:
            raise ValueError("Invalid suspected Harness layer: %s" % suspected)
        return {
            "status": status,
            "responsibility": None,
            "category_code": suspected,
            "category_name": HARNESS_LAYERS.get(suspected),
            "root_cause_step": None,
            "component": None,
            "summary": str(
                result.get("reason")
                or result.get("summary")
                or "证据不足，无法可靠归因。"
            ),
            "evidence": [],
            "causal_chain": [],
            "primary_cause": None,
            "secondary_factors": [],
            "failure_surface": None,
            "counterfactual": None,
            "rejected_candidates": result.get("rejected_candidates") or [],
            "missing_evidence": result.get("missing_evidence") or [],
            "contributing_factor": None,
            "confidence": "LOW",
            "evidence_validation_level": "NOT_APPLICABLE",
            **common,
        }
    if status != "ATTRIBUTED":
        raise ValueError(
            "status must be ATTRIBUTED, HARNESS_SUSPECTED, or UNDETERMINED"
        )

    primary = result.get("primary_cause")
    if not isinstance(primary, dict):
        raise ValueError("attributed reports require primary_cause")
    responsibility = str(primary.get("responsibility") or "").upper()
    category_code = str(primary.get("category_code") or "")
    category_name = validate_category(responsibility, category_code)
    valid_steps = {int(step["step_index"]): step for step in steps}
    root_step = primary.get("step_id")
    if responsibility == "LLM" and root_step not in valid_steps:
        raise ValueError("LLM root_cause_step must reference a real step")
    if root_step is not None and root_step not in valid_steps:
        raise ValueError("root_cause_step must reference a real step")

    evidence = result.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("attributed reports require evidence")
    verified = []
    facts_text = json.dumps(sanitize_value(facts), ensure_ascii=False)
    sanitized_log = str(sanitize_value(final_log))
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("evidence items must be objects")
        quote = str(item.get("quote") or "").strip()
        step_id = item.get("step_id")
        source = str(item.get("source") or "trajectory")
        supports_claim = str(item.get("supports_claim") or "").strip()
        relation = str(item.get("relation") or "").upper().replace("-", "_")
        relation = {
            "DIRECT": "DIRECT_SUPPORT",
            "SUPPORT": "DIRECT_SUPPORT",
            "CONTEXT": "CONTEXT_SUPPORT",
        }.get(relation, relation)
        if not quote:
            raise ValueError("evidence quote cannot be empty")
        if not supports_claim:
            raise ValueError("evidence supports_claim cannot be empty")
        if relation not in ("DIRECT_SUPPORT", "CONTEXT_SUPPORT"):
            raise ValueError(
                "evidence relation must be DIRECT_SUPPORT or CONTEXT_SUPPORT"
            )
        if step_id is not None:
            if step_id not in valid_steps:
                raise ValueError("evidence step_id must reference a real step")
            step = valid_steps[step_id]
            haystack = str(
                sanitize_value(
                    "%s\n%s\n%s"
                    % (
                        step.get("content") or "",
                        step.get("command") or "",
                        "\n".join(_string_values(step.get("tool_arguments"))),
                    )
                )
            )
            if not _quote_present(quote, haystack):
                raise ValueError("evidence quote is not present in step %s" % step_id)
            source = str(step.get("role") or source)
        elif not _quote_present(quote, sanitized_log) and not _quote_present(
            quote, facts_text
        ):
            raise ValueError("non-step evidence quote is not present in logs or facts")
        verified.append(
            {
                "source": source,
                "step_id": step_id,
                "quote": quote,
                "supports_claim": supports_claim,
                "relation": relation,
            }
        )

    confidence = str(result.get("confidence") or "").upper()
    if confidence not in CONFIDENCE_LEVELS:
        raise ValueError("confidence must be HIGH, MEDIUM, or LOW")
    secondary = result.get("secondary_factors") or []
    if not isinstance(secondary, list) or len(secondary) > 3:
        raise ValueError("secondary_factors must be a list with at most three items")
    causal_chain = result.get("causal_chain") or []
    if not isinstance(causal_chain, list) or not causal_chain:
        raise ValueError("attributed reports require a causal_chain")
    for event in causal_chain:
        if not isinstance(event, dict) or str(event.get("role") or "").upper() not in (
            "TRIGGER",
            "PROPAGATION",
            "SECONDARY",
            "FAILURE_SURFACE",
        ):
            raise ValueError("causal_chain contains an invalid event")
        event_step = event.get("step_id")
        if event_step is not None and event_step not in valid_steps:
            raise ValueError("causal_chain step_id must reference a real step")
    counterfactual = result.get("counterfactual")
    if (
        not isinstance(counterfactual, dict)
        or not counterfactual.get("intervention")
        or not counterfactual.get("expected_effect")
    ):
        raise ValueError("attributed reports require a counterfactual")
    claim = str(primary.get("claim") or "").strip()
    if not claim:
        raise ValueError("primary_cause claim cannot be empty")
    return {
        "status": status,
        "responsibility": responsibility,
        "category_code": category_code,
        "category_name": category_name,
        "root_cause_step": root_step,
        "component": str(primary.get("component") or "") or None,
        "summary": str(result.get("summary") or claim),
        "primary_cause": primary,
        "secondary_factors": secondary,
        "failure_surface": result.get("failure_surface"),
        "evidence": verified,
        "causal_chain": causal_chain,
        "counterfactual": counterfactual,
        "rejected_candidates": result.get("rejected_candidates") or [],
        "contributing_factor": (
            str(secondary[0].get("claim") or "") if secondary else None
        ),
        "confidence": confidence,
        "evidence_validation_level": "PROVENANCE_AND_REPORT_STRUCTURE",
        **common,
    }


def failed_diagnosis(
    error: Exception,
    model: Optional[str] = None,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
) -> Dict[str, Any]:
    return {
        "status": "FAILED",
        "responsibility": None,
        "category_code": None,
        "category_name": None,
        "root_cause_step": None,
        "component": None,
        "summary": "诊断服务执行失败。",
        "primary_cause": None,
        "secondary_factors": [],
        "failure_surface": None,
        "evidence": [],
        "causal_chain": [],
        "counterfactual": None,
        "rejected_candidates": [],
        "contributing_factor": None,
        "confidence": None,
        "decision_source": None,
        "matched_rule": None,
        "judge_model": model,
        "prompt_version": PROMPT_VERSION,
        "rule_version": RULE_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "diagnosis_config_hash": (
            diagnosis_config_hash(model, max_input_tokens) if model else None
        ),
        "judge_input_tokens": 0,
        "judge_output_tokens": 0,
        "judge_latency_seconds": 0.0,
        "judge_thinking": THINKING_LABEL,
        "judge_temperature": TEMPERATURE,
        "judge_call_count": 0,
        "trajectory_mode": None,
        "evidence_validation_level": "NOT_APPLICABLE",
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "max_input_tokens": max_input_tokens,
        "diagnosis_error": str(sanitize_value(str(error))),
    }


def unavailable_trajectory_diagnosis(
    model: Optional[str] = None,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
) -> Dict[str, Any]:
    result = failed_diagnosis(
        ValueError("Agent produced an outcome but no diagnostic trajectory"),
        model,
        max_input_tokens,
    )
    result.update(
        status="UNDETERMINED",
        summary="Verifier 已确认任务失败，但 Agent 没有产出可供归因的轨迹。",
        decision_source="RULE",
        matched_rule="trajectory_unavailable",
        judge_thinking="not_called",
        trajectory_mode="OUTCOME_ONLY",
    )
    result.pop("diagnosis_error", None)
    return result


def diagnose_outcome_only(
    raw_path: Path,
    verdict: str,
    health_status: str,
    model: Optional[str] = None,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    result_path = raw_path
    if raw_path.is_file():
        try:
            payload = read_json(raw_path)
        except (OSError, ValueError):
            payload = {}
    if raw_path.name != "result.json":
        alternative = raw_path.parent.parent / "result.json"
        if alternative.is_file():
            payload = read_json(alternative)
            result_path = alternative
    exception = payload.get("exception_info") or {}
    if health_status == "INFRA_ERROR" or verdict == "INFRA_ERROR":
        facts = {
            "verdict": verdict,
            "health_status": health_status,
            "missing_tool_results": [],
            "context_truncation_markers": [],
            "exception_type": str(exception.get("exception_type") or ""),
            "exception_message": str(
                exception.get("exception_message") or exception.get("message") or ""
            ),
            "result_path": str(result_path),
            "verifier_present": bool(payload.get("verifier_result")),
        }
        ruled = match_harness_rule(facts)
        if ruled:
            ruled["max_input_tokens"] = max_input_tokens
            ruled["diagnosis_config_hash"] = (
                diagnosis_config_hash(model, max_input_tokens) if model else None
            )
            ruled["trajectory_mode"] = "OUTCOME_ONLY"
            return ruled
    return unavailable_trajectory_diagnosis(model, max_input_tokens)


def _judge_payload(
    verdict: str,
    facts: Dict[str, Any],
    structured: Dict[str, Any],
    trajectory_view: Dict[str, Any],
    final_log: Any,
) -> Dict[str, Any]:
    return sanitize_value(
        {
            "task": "Diagnose this failed agent run under diagnosis protocol v3.",
            "verdict": verdict,
            "runtime_facts": facts,
            "structured_failure_facts": structured,
            "contractual_responsibility": {
                "llm": "Invalid tool choice/arguments or ignored valid feedback.",
                "harness": (
                    "Valid calls broken during transport, execution, result "
                    "injection, lifecycle, or verification."
                ),
            },
            "harness_layers": HARNESS_LAYERS,
            "llm_categories": LLM_CATEGORIES,
            "trajectory_view": trajectory_view,
            "verifier_log": final_log,
        }
    )


def _bounded_log(value: str, limit: int = 16_000) -> Any:
    if len(value) <= limit:
        return value
    half = limit // 2
    return {
        "content_truncated": True,
        "original_characters": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "beginning": value[:half],
        "ending": value[-half:],
    }


def _input_too_large(
    facts: Dict[str, Any], estimated: int, max_input_tokens: int, config_hash: str
) -> Dict[str, Any]:
    return {
        "status": "INPUT_TOO_LARGE",
        "responsibility": None,
        "category_code": None,
        "category_name": None,
        "root_cause_step": None,
        "component": None,
        "summary": "结构索引和关键原文仍超过 Judge 输入上限，未调用 Judge。",
        "primary_cause": None,
        "secondary_factors": [],
        "failure_surface": None,
        "evidence": [],
        "causal_chain": [],
        "counterfactual": None,
        "rejected_candidates": [],
        "contributing_factor": None,
        "confidence": None,
        "decision_source": "RULE",
        "matched_rule": "input_too_large",
        "judge_model": None,
        "prompt_version": PROMPT_VERSION,
        "rule_version": RULE_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "diagnosis_config_hash": config_hash,
        "judge_input_tokens": 0,
        "judge_output_tokens": 0,
        "judge_latency_seconds": 0.0,
        "judge_thinking": "not_called",
        "judge_temperature": TEMPERATURE,
        "judge_call_count": 0,
        "trajectory_mode": "HIERARCHICAL",
        "evidence_validation_level": "NOT_APPLICABLE",
        "max_output_tokens": 0,
        "estimated_input_tokens": estimated,
        "max_input_tokens": max_input_tokens,
        "runtime_facts": facts,
    }


def _requested_steps(result: Dict[str, Any], valid: set) -> List[int]:
    requested = set()
    for item in result.get("requested_step_ids") or []:
        try:
            step_id = int(item)
        except (TypeError, ValueError):
            continue
        if step_id in valid:
            requested.add(step_id)
    for item in result.get("requested_ranges") or []:
        if not isinstance(item, dict):
            continue
        try:
            start, end = int(item["start"]), int(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        for step_id in range(min(start, end), max(start, end) + 1):
            if step_id in valid:
                requested.add(step_id)
            if len(requested) >= 100:
                break
    return sorted(requested)


def _combined_usage(
    first: Dict[str, Any], second: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    second = second or {}
    return {
        "input_tokens": int(first.get("input_tokens") or 0)
        + int(second.get("input_tokens") or 0),
        "output_tokens": int(first.get("output_tokens") or 0)
        + int(second.get("output_tokens") or 0),
        "latency_seconds": float(first.get("latency_seconds") or 0)
        + float(second.get("latency_seconds") or 0),
        "call_count": 2 if second else 1,
    }


def analyze_trajectory(
    raw_path: Path,
    verdict: str,
    health_status: str,
    final_log: str = "",
    model: str = "deepseek-v4-pro",
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    client: Any = None,
) -> Dict[str, Any]:
    steps = normalize_trajectory(read_json(raw_path))
    facts = runtime_facts(raw_path, verdict, health_status, final_log, steps)
    structured = structured_failure_facts(steps, facts, final_log)
    config_hash = diagnosis_config_hash(model, max_input_tokens)
    ruled = match_harness_rule(facts)
    if ruled:
        ruled["max_input_tokens"] = max_input_tokens
        ruled["diagnosis_config_hash"] = config_hash
        ruled["structured_failure_facts"] = structured
        return ruled

    system = PROMPT_PATH.read_text(encoding="utf-8")
    full_view = {
        "mode": "FULL",
        "total_steps": len(steps),
        "steps": [summarize_step(step, None) for step in steps],
        "coverage": {"all_steps_included": True},
    }
    payload = _judge_payload(verdict, facts, structured, full_view, final_log)
    estimated = estimate_tokens(system) + estimate_tokens(payload)
    trajectory_mode = "FULL"
    if estimated > max_input_tokens:
        trajectory_mode = "HIERARCHICAL"
        view = hierarchical_trajectory_view(steps, structured)
        bounded_log = _bounded_log(final_log)
        payload = _judge_payload(verdict, facts, structured, view, bounded_log)
        estimated = estimate_tokens(system) + estimate_tokens(payload)
        if estimated > max_input_tokens:
            return _input_too_large(facts, estimated, max_input_tokens, config_hash)

    if client is None:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required for LLM diagnosis")
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    raw_result, first_usage = _json_call(client, model, system, payload)
    usage = _combined_usage(first_usage)

    if (
        trajectory_mode == "HIERARCHICAL"
        and str(raw_result.get("status") or "").upper() == "NEED_MORE_EVIDENCE"
    ):
        valid = {int(step["step_index"]) for step in steps}
        requested = _requested_steps(raw_result, valid)
        if not requested:
            raw_result = {
                "status": "UNDETERMINED",
                "reason": "Judge 请求补充证据，但没有提供有效步骤范围。",
                "missing_evidence": raw_result.get("missing_evidence") or [],
            }
        else:
            expanded_view = hierarchical_trajectory_view(steps, structured, requested)
            expanded_view["previous_request"] = {
                "requested_step_ids": requested,
                "reason": raw_result.get("reason"),
            }
            second_payload = _judge_payload(
                verdict, facts, structured, expanded_view, bounded_log
            )
            second_estimated = estimate_tokens(system) + estimate_tokens(second_payload)
            if second_estimated > max_input_tokens:
                raw_result = {
                    "status": "UNDETERMINED",
                    "reason": "请求补充的原始步骤超过输入上限。",
                    "missing_evidence": raw_result.get("missing_evidence") or [],
                }
            else:
                second_result, second_usage = _json_call(
                    client, model, system, second_payload
                )
                usage = _combined_usage(first_usage, second_usage)
                raw_result = second_result
                estimated = second_estimated
                if str(raw_result.get("status") or "").upper() == "NEED_MORE_EVIDENCE":
                    raw_result = {
                        "status": "UNDETERMINED",
                        "reason": "两次受控诊断后证据仍然不足。",
                        "missing_evidence": raw_result.get("missing_evidence") or [],
                    }

    report = _validate_llm_report(
        raw_result,
        steps,
        final_log,
        facts,
        model,
        usage,
        trajectory_mode,
        config_hash,
    )
    report.update(
        {
            "estimated_input_tokens": estimated,
            "max_input_tokens": max_input_tokens,
            "structured_failure_facts": structured,
        }
    )
    return report
