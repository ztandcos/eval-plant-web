import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core import (
    CONFIDENCE_LEVELS,
    HARNESS_LAYERS,
    LLM_CATEGORIES,
    estimate_tokens,
    normalize_trajectory,
    read_json,
    signal_bundle,
    summarize_step,
    validate_category,
)

PROMPT_PATH = Path(__file__).with_name("diagnosis_prompt.txt")
PROMPT_VERSION = "engineering_diagnosis_v2"
DEFAULT_MAX_INPUT_TOKENS = 100_000
DEFAULT_MAX_OUTPUT_TOKENS = 4_096
THINKING_CONFIG = "disabled"


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
    missing = [{"tool_call_id": key, **value} for key, value in calls.items() if key not in results]
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
        "result_path": str(result_path) if result_path else None,
        "verifier_present": bool(result.get("verifier_result")),
        "exception_type": str(exception.get("exception_type") or ""),
        "exception_message": str(exception.get("exception_message") or exception.get("message") or ""),
        "missing_tool_results": missing,
        "context_truncation_markers": truncation_markers[:3],
        "signals": signal_bundle(steps),
    }


def _rule_evidence(source: str, quote: str, explanation: str) -> List[Dict[str, Any]]:
    return [{"source": source, "step_id": None, "quote": quote, "explanation": explanation}]


def _rule_report(
    code: str,
    component: str,
    matched_rule: str,
    summary: str,
    evidence: List[Dict[str, Any]],
    facts: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "status": "ATTRIBUTED",
        "responsibility": "HARNESS",
        "category_code": code,
        "category_name": HARNESS_LAYERS[code],
        "root_cause_step": None,
        "component": component,
        "summary": summary,
        "evidence": evidence,
        "causal_chain": [summary],
        "contributing_factor": None,
        "confidence": "HIGH",
        "decision_source": "RULE",
        "matched_rule": matched_rule,
        "judge_model": None,
        "prompt_version": PROMPT_VERSION,
        "judge_input_tokens": 0,
        "judge_output_tokens": 0,
        "judge_latency_seconds": 0.0,
        "judge_thinking": "not_called",
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
            _rule_evidence("database", "health_status=INCOMPLETE", "导入记录标记运行不完整。"),
            facts,
        )

    if facts["verdict"] == "UNKNOWN" and not facts["verifier_present"]:
        return _rule_report(
            "H-V",
            "verifier",
            "verifier_missing",
            "任务没有可用的 Verifier 结果，无法判断是否完成。",
            _rule_evidence("result.json", "verifier_result missing", "结果文件缺少 Verifier 结论。"),
            facts,
        )

    # A normal agent timeout can be caused by the model looping. Only explicit
    # infrastructure failures are classified from exception text here.
    if facts["health_status"] != "INFRA_ERROR":
        return None

    if re.search(r"verifier|grader|reward|test[-_ ]?runner", lowered):
        code, component, rule = "H-V", "verifier", "verifier_exception"
    elif re.search(r"tool|function|mcp|schema|argument", lowered):
        code, component, rule = "H-T", "tool_adapter", "tooling_exception"
    elif re.search(r"context|prompt|history|memory|token.{0,20}(?:limit|length)|truncat", lowered):
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


def _json_call(client: Any, model: str, system: str, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    started = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        extra_body={"thinking": {"type": "disabled"}},
    )
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


def _validate_llm_report(
    result: Dict[str, Any],
    steps: List[Dict[str, Any]],
    final_log: str,
    facts: Dict[str, Any],
    model: str,
    usage: Dict[str, Any],
) -> Dict[str, Any]:
    status = str(result.get("status") or "").upper()
    if status == "UNDETERMINED":
        return {
            "status": status,
            "responsibility": None,
            "category_code": None,
            "category_name": None,
            "root_cause_step": None,
            "component": None,
            "summary": str(result.get("summary") or "证据不足，无法可靠归因。"),
            "evidence": [],
            "causal_chain": [],
            "contributing_factor": None,
            "confidence": "LOW",
            "decision_source": "LLM",
            "matched_rule": None,
            "judge_model": model,
            "prompt_version": PROMPT_VERSION,
            "judge_input_tokens": usage["input_tokens"],
            "judge_output_tokens": usage["output_tokens"],
            "judge_latency_seconds": usage["latency_seconds"],
            "judge_thinking": THINKING_CONFIG,
            "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            "runtime_facts": facts,
        }
    if status != "ATTRIBUTED":
        raise ValueError("status must be ATTRIBUTED or UNDETERMINED")

    responsibility = str(result.get("responsibility") or "").upper()
    category_code = str(result.get("category_code") or "")
    category_name = validate_category(responsibility, category_code)
    valid_steps = {int(step["step_index"]): step for step in steps}
    root_step = result.get("root_cause_step")
    if responsibility == "LLM" and root_step not in valid_steps:
        raise ValueError("LLM root_cause_step must reference a real step")
    if root_step is not None and root_step not in valid_steps:
        raise ValueError("root_cause_step must reference a real step")

    evidence = result.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("attributed reports require evidence")
    verified = []
    facts_text = json.dumps(facts, ensure_ascii=False)
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("evidence items must be objects")
        quote = str(item.get("quote") or "").strip()
        step_id = item.get("step_id")
        source = str(item.get("source") or "trajectory")
        if not quote:
            raise ValueError("evidence quote cannot be empty")
        if step_id is not None:
            if step_id not in valid_steps:
                raise ValueError("evidence step_id must reference a real step")
            step = valid_steps[step_id]
            haystack = "%s\n%s" % (step.get("content") or "", step.get("command") or "")
            if quote not in haystack:
                raise ValueError("evidence quote is not present in step %s" % step_id)
            source = str(step.get("role") or source)
        elif quote not in final_log and quote not in facts_text:
            raise ValueError("non-step evidence quote is not present in logs or facts")
        verified.append(
            {
                "source": source,
                "step_id": step_id,
                "quote": quote,
                "explanation": str(item.get("explanation") or ""),
            }
        )

    confidence = str(result.get("confidence") or "").upper()
    if confidence not in CONFIDENCE_LEVELS:
        raise ValueError("confidence must be HIGH, MEDIUM, or LOW")
    contributor = result.get("contributing_factor")
    if isinstance(contributor, list):
        if len(contributor) > 1:
            raise ValueError("at most one contributing_factor is allowed")
        contributor = contributor[0] if contributor else None
    return {
        "status": status,
        "responsibility": responsibility,
        "category_code": category_code,
        "category_name": category_name,
        "root_cause_step": root_step,
        "component": str(result.get("component") or "") or None,
        "summary": str(result.get("summary") or ""),
        "evidence": verified,
        "causal_chain": result.get("causal_chain") or [],
        "contributing_factor": contributor,
        "confidence": confidence,
        "decision_source": "LLM",
        "matched_rule": None,
        "judge_model": model,
        "prompt_version": PROMPT_VERSION,
        "judge_input_tokens": usage["input_tokens"],
        "judge_output_tokens": usage["output_tokens"],
        "judge_latency_seconds": usage["latency_seconds"],
        "judge_thinking": THINKING_CONFIG,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "runtime_facts": facts,
    }


def failed_diagnosis(error: Exception, model: Optional[str] = None) -> Dict[str, Any]:
    return {
        "status": "FAILED",
        "responsibility": None,
        "category_code": None,
        "category_name": None,
        "root_cause_step": None,
        "component": None,
        "summary": "诊断服务执行失败。",
        "evidence": [],
        "causal_chain": [],
        "contributing_factor": None,
        "confidence": None,
        "decision_source": None,
        "matched_rule": None,
        "judge_model": model,
        "prompt_version": PROMPT_VERSION,
        "judge_input_tokens": 0,
        "judge_output_tokens": 0,
        "judge_latency_seconds": 0.0,
        "judge_thinking": THINKING_CONFIG,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "diagnosis_error": str(error),
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
    ruled = match_harness_rule(facts)
    if ruled:
        ruled["max_input_tokens"] = max_input_tokens
        return ruled

    timeline = [summarize_step(step, None) for step in steps]
    payload = {
        "task": "Diagnose this failed agent run.",
        "verdict": verdict,
        "runtime_facts": facts,
        "harness_layers": HARNESS_LAYERS,
        "llm_categories": LLM_CATEGORIES,
        "full_timeline": timeline,
        "verifier_log": final_log,
    }
    system = PROMPT_PATH.read_text(encoding="utf-8")
    estimated = estimate_tokens(system) + estimate_tokens(payload)
    if estimated > max_input_tokens:
        return {
            "status": "INPUT_TOO_LARGE",
            "responsibility": None,
            "category_code": None,
            "category_name": None,
            "root_cause_step": None,
            "component": None,
            "summary": "完整轨迹超过 Judge 输入上限，未进行截断或调用。",
            "evidence": [],
            "causal_chain": [],
            "contributing_factor": None,
            "confidence": None,
            "decision_source": "RULE",
            "matched_rule": "input_too_large",
            "judge_model": None,
            "prompt_version": PROMPT_VERSION,
            "judge_input_tokens": 0,
            "judge_output_tokens": 0,
            "judge_latency_seconds": 0.0,
            "judge_thinking": "not_called",
            "max_output_tokens": 0,
            "estimated_input_tokens": estimated,
            "max_input_tokens": max_input_tokens,
            "runtime_facts": facts,
        }

    if client is None:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required for LLM diagnosis")
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    raw_result, usage = _json_call(client, model, system, payload)
    report = _validate_llm_report(raw_result, steps, final_log, facts, model, usage)
    report["estimated_input_tokens"] = estimated
    report["max_input_tokens"] = max_input_tokens
    return report
