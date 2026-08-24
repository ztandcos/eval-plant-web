import json
import os
from pathlib import Path
from typing import Any, Dict

from .core import (
    TAXONOMY,
    normalize_trajectory,
    read_json,
    signal_bundle,
    summarize_step,
    validate_taxonomy,
)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "attribution_v1.txt"


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


def _validate_attribution(result: Dict[str, Any], valid_steps: set) -> Dict[str, Any]:
    if not isinstance(result.get("attributable"), bool):
        raise ValueError("attributable must be boolean")
    confidence = float(result.get("confidence") or 0)
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between zero and one")
    if not result["attributable"]:
        return {
            "attributable": False,
            "first_error_step": None,
            "stage": None,
            "mechanism": None,
            "subcategory": None,
            "summary": str(result.get("summary") or "Insufficient evidence"),
            "evidence_step_ids": [],
            "confidence": confidence,
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
    phase = str(result.get("stage"))
    category = str(result.get("mechanism"))
    subcategory = str(result.get("subcategory"))
    validate_taxonomy(phase, category, subcategory)
    return {
        "attributable": True,
        "first_error_step": step,
        "stage": phase,
        "mechanism": category,
        "subcategory": subcategory,
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
    evidence = signal_bundle(steps)
    evidence.update(
        {
            "final_patch_present": bool(final_patch.strip()),
            "final_patch_chars": len(final_patch),
            "verifier_log_tail": final_log[-12000:],
        }
    )
    payload = {
        "task": "Find the earliest evidence-supported pivotal agent error.",
        "taxonomy": TAXONOMY,
        "deterministic_evidence": evidence,
        "full_timeline": [summarize_step(step) for step in steps],
        "final_patch": final_patch[:30000],
    }
    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    result = _validate_attribution(
        _json_call(
            client,
            model,
            PROMPT_PATH.read_text(encoding="utf-8"),
            payload,
        ),
        valid_steps,
    )
    result["deterministic_evidence"] = evidence
    return result
