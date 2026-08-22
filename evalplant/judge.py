import json
import os
from pathlib import Path
from typing import Any, Dict, List

from .core import (
    MECHANISMS,
    STAGES,
    normalize_trajectory,
    read_json,
    signal_bundle,
    summarize_step,
    validate_stage_mechanism,
)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _json_call(
    client: Any, model: str, system: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        extra_body={"thinking": {"type": "enabled"}},
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("Judge returned an empty response")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("Judge response must be a JSON object")
    return result


def _candidate_steps(result: Dict[str, Any], valid_steps: set) -> List[int]:
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 3:
        raise ValueError("Judge must return one to three candidates")
    step_ids = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(
            candidate.get("step_id"), int
        ):
            raise ValueError("Each candidate needs an integer step_id")
        if candidate["step_id"] not in valid_steps:
            raise ValueError("Candidate references an unknown step")
        step_ids.append(candidate["step_id"])
    return step_ids


def _validate_attribution(result: Dict[str, Any], valid_steps: set) -> Dict[str, Any]:
    if not isinstance(result.get("attributable"), bool):
        raise ValueError("attributable must be boolean")
    if not result["attributable"]:
        return {
            "attributable": False,
            "first_error_step": None,
            "stage": None,
            "mechanism": None,
            "summary": str(result.get("summary") or "No supported attribution"),
            "evidence_step_ids": [],
            "confidence": float(result.get("confidence") or 0),
            "uncertainty": result.get("uncertainty"),
        }

    step = result.get("first_error_step")
    evidence = result.get("evidence_step_ids")
    if not isinstance(step, int) or step not in valid_steps:
        raise ValueError("first_error_step must reference a real step")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(item not in valid_steps for item in evidence)
    ):
        raise ValueError("evidence_step_ids must reference real steps")
    validate_stage_mechanism(str(result.get("stage")), str(result.get("mechanism")))
    confidence = float(result.get("confidence"))
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between zero and one")
    return {
        "attributable": True,
        "first_error_step": step,
        "stage": result["stage"],
        "mechanism": result["mechanism"],
        "summary": str(result.get("summary") or ""),
        "evidence_step_ids": evidence,
        "counter_evidence": result.get("counter_evidence") or [],
        "confidence": confidence,
        "uncertainty": result.get("uncertainty"),
    }


def analyze_trajectory(
    raw_path: Path,
    final_patch: str = "",
    final_log: str = "",
    model: str = "deepseek-v4-pro",
) -> Dict[str, Any]:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY is required")
    from openai import OpenAI

    steps = normalize_trajectory(read_json(raw_path))
    valid_steps = {step["step_index"] for step in steps}
    if not valid_steps:
        raise ValueError("Cannot analyze an empty trajectory")
    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )

    candidate_payload = {
        "task": (
            "Locate up to three candidate steps for the earliest pivotal error "
            "in this failed coding-agent trajectory."
        ),
        "signals": signal_bundle(steps),
        "timeline": [summarize_step(step) for step in steps],
    }
    candidate_prompt = (PROMPT_DIR / "candidate_v1.txt").read_text(encoding="utf-8")
    candidate_result = _json_call(client, model, candidate_prompt, candidate_payload)
    candidate_ids = _candidate_steps(candidate_result, valid_steps)

    neighborhoods = []
    for candidate_id in candidate_ids:
        neighborhoods.append(
            {
                "candidate": candidate_id,
                "steps": [
                    summarize_step(step, limit=5000)
                    for step in steps
                    if candidate_id - 2 <= step["step_index"] <= candidate_id + 2
                ],
            }
        )
    attribution_payload = {
        "task": (
            "Identify the earliest pivotal error that materially contributed "
            "to the final failure."
        ),
        "allowed_stages": STAGES,
        "allowed_mechanisms": MECHANISMS,
        "candidate_neighborhoods": neighborhoods,
        "terminal_steps": [
            summarize_step(step, limit=2000)
            for step in steps
            if step.get("role") == "exit"
        ],
        "final_patch": final_patch[:20000],
        "final_test_log": final_log[-20000:],
    }
    attribution_prompt = (PROMPT_DIR / "attribution_v1.txt").read_text(encoding="utf-8")
    last_error = None
    for _ in range(2):
        try:
            result = _validate_attribution(
                _json_call(client, model, attribution_prompt, attribution_payload),
                valid_steps,
            )
            result["candidate_analysis"] = candidate_result
            return result
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            last_error = error
            attribution_payload["validation_error"] = str(error)
    raise ValueError("Judge output stayed invalid: %s" % last_error)
