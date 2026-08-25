import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core import normalize_trajectory, signal_bundle, summarize_step

ROOT = Path(__file__).resolve().parent.parent
PROMPT_VERSION = "two_pass_v2"
UNIVERSAL_PROMPT_VERSION = "universal_attribution_v1"
UNIVERSAL_PROMPT = ROOT / "prompts" / "attribution_universal_v1.txt"
UNIVERSAL_METHODS = ("raw_direct", "graph_attribution", "g_rav")
OLLAMA_CONFIG = {
    "temperature": 0,
    "seed": 7,
    "thinking": False,
    "num_ctx": 32768,
    "max_tokens": 4096,
}
CANDIDATE_PROMPT = ROOT / "prompts" / "attribution_candidates_v2.txt"
VERIFY_PROMPT = ROOT / "prompts" / "attribution_verify_v2.txt"
CANDIDATE_CALL_CONFIG = {
    "temperature": 0,
    "thinking": {"type": "disabled"},
    "max_tokens": 1024,
}
VERIFY_CALL_CONFIG = {
    "temperature": 0,
    "thinking": {"type": "enabled"},
    "reasoning_effort": "high",
    "max_tokens": 16384,
}
DOMAINS = {"agent_model", "product_infra", "user_task_mismatch", "unknown_mixed"}
FAILURE_MODES = {
    "task_understanding",
    "planning_or_coordination",
    "information_or_reasoning",
    "tool_or_action_execution",
    "verification_or_completion",
    "product_or_environment",
    "user_task_issue",
    "unknown",
}
CANDIDATE_CLASSIFICATIONS = {
    "pivotal_root_cause",
    "non_causal_imperfection",
    "failure_symptom",
    "repair_after_failure",
    "insufficient_evidence",
}


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _judge_config() -> Dict[str, Any]:
    return {
        "candidate": {
            "prompt_file": CANDIDATE_PROMPT.name,
            "prompt_sha256": hashlib.sha256(CANDIDATE_PROMPT.read_bytes()).hexdigest(),
            **CANDIDATE_CALL_CONFIG,
        },
        "verify": {
            "prompt_file": VERIFY_PROMPT.name,
            "prompt_sha256": hashlib.sha256(VERIFY_PROMPT.read_bytes()).hexdigest(),
            **VERIFY_CALL_CONFIG,
        },
    }


def _clean(value: Any, limit: int = 1200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _salient_content(value: Any, limit: int) -> str:
    text = str(value or "")
    code = re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
    if not code:
        return _clean(text, limit)
    code_text = _clean("\n".join(code), max(80, int(limit * 0.7)))
    context = _clean(text.split("```", 1)[0], max(80, limit - len(code_text) - 16))
    return _clean("CODE: %s CONTEXT: %s" % (code_text, context), limit)


def _case_id(path: Path, source: Path) -> str:
    relative = path.relative_to(source) if source.is_dir() else Path(path.name)
    stem = "-".join(relative.with_suffix("").parts).lower()
    return "who-when-" + re.sub(r"[^a-z0-9-]+", "-", stem).strip("-")


def convert_who_when(source: Path, output: Path, limit: int = 0) -> Dict[str, Any]:
    """Convert public Who&When JSON files while keeping gold labels separate."""
    files = [source] if source.is_file() else sorted(source.rglob("*.json"))
    records = []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("history"), list):
            continue
        if data.get("mistake_step") in (None, ""):
            continue
        records.append((path, data))
    if not records:
        raise ValueError("No Who&When records found under %s" % source)

    cases_dir = output / "cases"
    labels_dir = output / "labels"
    for directory in (cases_dir, labels_dir):
        if directory.exists():
            for stale in directory.glob("who-when-*.json"):
                stale.unlink()
    converted = 0
    split_counts = {"dev": 0, "test": 0}
    seen = set()
    for path, data in records:
        case_id = _case_id(path, source)
        if case_id in seen:
            raise ValueError("Duplicate converted case ID: %s" % case_id)
        seen.add(case_id)
        source_step = int(data["mistake_step"])
        steps = []
        for index, message in enumerate(data["history"], start=1):
            role = str(message.get("role") or "unknown")
            original_actor = str(message.get("name") or role)
            actor_key = re.sub(r"[^a-z]+", "_", original_actor.lower()).strip("_")
            is_tool = role == "tool" or actor_key in {
                "computer_terminal",
                "terminal",
                "tool",
            }
            steps.append(
                {
                    "step_id": index,
                    "source": "tool" if is_tool else "agent",
                    "actor": "tool" if is_tool else "agent",
                    "message": str(message.get("content") or ""),
                    "source_step_id": index - 1,
                }
            )
        digest = int(hashlib.sha256(case_id.encode()).hexdigest()[:8], 16)
        split = "dev" if digest % 5 == 0 else "test"
        case = {
            "schema_version": "ATIF-v1.7",
            "session_id": case_id,
            "task": str(data.get("question") or ""),
            "system_prompt": str(data.get("system_prompt") or ""),
            "outcome": {"status": "FAIL"},
            "agent": {"name": "single-agent-normalized"},
            "provenance": {
                "dataset": "Who&When",
                "split": split,
                "source_file": str(path),
                "source_question_id": data.get("question_ID"),
                "license": "MIT",
                "converter_version": "who_when_v1",
                "actor_policy": "collapse_non_tool_to_agent",
            },
            "steps": steps,
        }
        label = {
            "case_id": case_id,
            "split": split,
            "source_mistake_step": source_step,
            "first_error_step": source_step + 1,
            "mistake_agent": data.get("mistake_agent"),
            "actor_evaluated": False,
            "mistake_reason": data.get("mistake_reason"),
            "ground_truth": data.get("ground_truth"),
            "source_is_correct": data.get("is_correct", data.get("is_corrected")),
        }
        _write_json(cases_dir / (case_id + ".json"), case)
        _write_json(labels_dir / (case_id + ".json"), label)
        converted += 1
        split_counts[split] += 1
        if limit and converted >= limit:
            break
    manifest = {
        "dataset": "Who&When",
        "license": "MIT",
        "converted": converted,
        "splits": split_counts,
        "gold_is_separate": True,
        "converter_version": "who_when_v1",
    }
    _write_json(output / "manifest.json", manifest)
    return manifest


def _artifact_paths(command: str) -> List[str]:
    return sorted(
        set(
            re.findall(
                r"(?:[\w.-]+/)*[\w.-]+\.(?:py|sh|json|ya?ml|toml|txt|md|csv)",
                command or "",
            )
        )
    )


def _facts(text: str) -> set:
    numbers = re.findall(r"(?<!\w)-?\d+(?:\.\d+)?(?!\w)", text or "")
    facts = {
        number for number in numbers if "." in number or len(number.lstrip("-")) >= 2
    }
    for line in str(text or "").splitlines():
        line = line.strip()
        if 2 <= len(line) <= 80 and re.fullmatch(r"[\w./:@+-]+", line):
            facts.add(line)
    return facts


def build_attribution_graph(case: Dict[str, Any]) -> Dict[str, Any]:
    """Build a small deterministic graph. No model call occurs here."""
    steps = normalize_trajectory(case)
    raw_steps = {
        int(step.get("step_id")): step
        for step in case.get("steps", [])
        if step.get("step_id") is not None
    }
    nodes: List[Dict[str, Any]] = [
        {
            "id": "task",
            "type": "Task",
            "step_id": None,
            "content": _clean(case.get("task"), 1600),
            "source_ref": "task",
        }
    ]
    edges: List[Dict[str, Any]] = []
    artifacts: Dict[str, str] = {}
    for position, step in enumerate(steps):
        step_id = step["step_index"]
        raw = raw_steps.get(step_id, {})
        role = step.get("role")
        if role == "tool":
            node_type = "ToolResult"
        elif step.get("action_type") in {
            "shell_command",
            "file_edit",
            "file_read",
            "test_execution",
        }:
            node_type = "AgentAction"
        else:
            node_type = "AgentStep"
        node_id = "step-%s" % step_id
        nodes.append(
            {
                "id": node_id,
                "type": node_type,
                "step_id": step_id,
                "actor": raw.get("actor") or role,
                "action": step.get("action_type"),
                "content": _salient_content(step.get("content"), 1600),
                "source_ref": "steps.step_id=%s" % step_id,
            }
        )
        previous = steps[position - 1] if position else None
        if previous is None:
            edges.append(
                {
                    "from": "task",
                    "to": node_id,
                    "relation": "CONTEXT_FOR",
                    "extractor": "structure",
                }
            )
        else:
            previous_id = "step-%s" % previous["step_index"]
            edges.append(
                {
                    "from": previous_id,
                    "to": node_id,
                    "relation": "NEXT",
                    "extractor": "structure",
                }
            )
            previous_raw = raw_steps.get(previous["step_index"], {})
            previous_actor = str(
                previous_raw.get("actor") or previous.get("role") or "unknown"
            )
            current_actor = str(raw.get("actor") or role or "unknown")
            if previous_actor != current_actor:
                edges.append(
                    {
                        "from": previous_id,
                        "to": node_id,
                        "relation": "HANDOFF",
                        "extractor": "actor_transition",
                    }
                )
            if role == "tool" and previous.get("role") in ("agent", "assistant"):
                edges.append(
                    {
                        "from": previous_id,
                        "to": node_id,
                        "relation": "RETURNS",
                        "extractor": "role_pair",
                    }
                )
            elif previous.get("role") == "tool" and role in ("agent", "assistant"):
                edges.append(
                    {
                        "from": previous_id,
                        "to": node_id,
                        "relation": "INFORMS",
                        "extractor": "role_pair",
                    }
                )
        for artifact in _artifact_paths(str(step.get("command") or "")):
            artifact_id = artifacts.get(artifact)
            if artifact_id is None:
                artifact_id = (
                    "artifact-" + hashlib.sha1(artifact.encode()).hexdigest()[:10]
                )
                artifacts[artifact] = artifact_id
                nodes.append(
                    {
                        "id": artifact_id,
                        "type": "ArtifactState",
                        "step_id": None,
                        "content": artifact,
                        "source_ref": node_id,
                    }
                )
            relation = "WRITES" if step.get("action_type") == "file_edit" else "READS"
            edges.append(
                {
                    "from": node_id,
                    "to": artifact_id,
                    "relation": relation,
                    "extractor": "command_path",
                }
            )
    for index, step in enumerate(steps):
        facts = _facts(str(step.get("content") or ""))
        if not facts:
            continue
        for later in steps[index + 1 : index + 4]:
            if later.get("role") not in ("agent", "assistant"):
                continue
            later_text = str(later.get("content") or "")
            matches = sorted(fact for fact in facts if fact and fact in later_text)
            if matches:
                edges.append(
                    {
                        "from": "step-%s" % step["step_index"],
                        "to": "step-%s" % later["step_index"],
                        "relation": "DATA_DEPENDENCY",
                        "extractor": "exact_fact_reuse",
                        "facts": matches[:5],
                    }
                )
                break
    outcome_id = "outcome"
    nodes.append(
        {
            "id": outcome_id,
            "type": "Outcome",
            "step_id": None,
            "content": _clean((case.get("outcome") or {}).get("status") or "FAIL"),
            "source_ref": "outcome",
        }
    )
    edges.append(
        {
            "from": "step-%s" % steps[-1]["step_index"] if steps else "task",
            "to": outcome_id,
            "relation": "LEADS_TO",
            "extractor": "structure",
        }
    )
    actor_steps: Dict[str, List[int]] = {}
    for step_id, raw in raw_steps.items():
        actor = str(raw.get("actor") or raw.get("source") or "unknown")
        actor_steps.setdefault(actor, []).append(step_id)
    signals: Dict[str, Any] = {"step_count": len(steps), "actor_steps": actor_steps}
    if (case.get("provenance") or {}).get("dataset") != "Who&When":
        signals["execution"] = signal_bundle(steps)
    return {
        "schema_version": "AttributionGraph-v1",
        "case_id": case.get("session_id"),
        "nodes": nodes,
        "edges": edges,
        "signals": signals,
    }


def _timeline(
    steps: List[Dict[str, Any]], max_chars: int, salient: bool = False
) -> List[Dict[str, Any]]:
    per_step = max(160, min(1600, max_chars // max(1, len(steps))))
    timeline = []
    for step in steps:
        compact = summarize_step(step, per_step)
        if salient:
            compact["content"] = _salient_content(step.get("content"), per_step)
        compact["actor"] = step.get("actor")
        timeline.append(compact)
    return timeline


def _graph_view(graph: Dict[str, Any], max_chars: int) -> Dict[str, Any]:
    step_nodes = [node for node in graph["nodes"] if node.get("step_id") is not None]
    per_node = max(160, min(420, max_chars // max(1, len(step_nodes))))
    nodes = []
    for node in graph["nodes"]:
        if node["type"] in {"Task", "Outcome"}:
            continue
        compact = {
            key: node.get(key)
            for key in ("id", "type", "step_id", "actor", "action")
            if node.get(key) is not None
        }
        compact["content"] = _clean(node.get("content"), per_node)
        nodes.append(compact)
    useful_edges = [
        edge
        for edge in graph["edges"]
        if edge["relation"] not in {"NEXT", "CONTEXT_FOR", "LEADS_TO", "HANDOFF"}
    ]
    edges = [
        {key: edge[key] for key in ("from", "to", "relation", "facts") if key in edge}
        for edge in useful_edges
    ]
    return {"nodes": nodes, "edges": edges, "signals": graph["signals"]}


def _usage(response: Any, elapsed: float) -> Dict[str, Any]:
    usage = getattr(response, "usage", None)

    def value(name: str) -> Any:
        return getattr(usage, name, 0) if usage is not None else 0

    return {
        "input_tokens": int(value("prompt_tokens") or 0),
        "output_tokens": int(value("completion_tokens") or 0),
        "total_tokens": int(value("total_tokens") or 0),
        "latency_seconds": elapsed,
        "finish_reason": getattr(response.choices[0], "finish_reason", None),
        "system_fingerprint": getattr(response, "system_fingerprint", None),
    }


def _judge_call(
    client: Any,
    model: str,
    system_path: Path,
    payload: Dict[str, Any],
    config: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    started = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_path.read_text(encoding="utf-8")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=config["temperature"],
        max_tokens=config["max_tokens"],
        extra_body={"thinking": config["thinking"]},
        **(
            {"reasoning_effort": config["reasoning_effort"]}
            if config.get("reasoning_effort")
            else {}
        ),
    )
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise ValueError(
            "Judge reached max_tokens before finishing JSON; increase the call budget"
        )
    content = choice.message.content
    if not content:
        raise ValueError("Judge returned an empty response")
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("Judge response must be a JSON object")
    return parsed, _usage(response, time.monotonic() - started)


def _candidates(result: Dict[str, Any], valid_steps: set) -> List[Dict[str, Any]]:
    raw = result.get("candidates")
    if not isinstance(raw, list):
        raise ValueError("Candidate Judge must return candidates")
    candidates = []
    seen = set()
    for item in raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("step_id"), int)
            or isinstance(item.get("step_id"), bool)
        ):
            continue
        step_id = item["step_id"]
        if step_id not in valid_steps or step_id in seen:
            continue
        candidates.append(
            {
                "step_id": step_id,
                "hypothesis": str(item.get("hypothesis") or ""),
                "evidence_hint": str(item.get("evidence_hint") or ""),
            }
        )
        seen.add(step_id)
        if len(candidates) == 3:
            break
    return candidates


def _evidence_packets(
    steps: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    method: str,
    graph: Dict[str, Any],
    max_chars: int,
) -> Dict[str, Any]:
    by_id = {step["step_index"]: step for step in steps}
    selected = set()
    candidate_ids = {item["step_id"] for item in candidates}
    if method == "raw":
        ordered = [step["step_index"] for step in steps]
        for candidate in candidate_ids:
            position = ordered.index(candidate)
            selected.update(ordered[max(0, position - 1) : position + 2])
        edges: List[Dict[str, Any]] = []
    else:
        node_ids = {"step-%s" % item for item in candidate_ids}
        selected.update(candidate_ids)
        relevant_edges = []
        for edge in graph["edges"]:
            if edge["from"] in node_ids or edge["to"] in node_ids:
                relevant_edges.append(edge)
                for endpoint in (edge["from"], edge["to"]):
                    if endpoint.startswith("step-"):
                        selected.add(int(endpoint.split("-", 1)[1]))
        edges = relevant_edges
    packet_steps = [by_id[item] for item in sorted(selected) if item in by_id]
    return {"steps": _timeline(packet_steps, max_chars, salient=True), "edges": edges}


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _candidate_reviews(
    result: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    by_id: Dict[int, Dict[str, str]],
) -> List[Dict[str, Any]]:
    raw = result.get("candidate_reviews")
    if not isinstance(raw, list) or len(raw) != len(candidates):
        raise ValueError("candidate_reviews must review every candidate exactly once")
    expected = {item["step_id"] for item in candidates}
    reviews = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("candidate review must be an object")
        step_id = item.get("step_id")
        if step_id not in expected or step_id in seen:
            raise ValueError("candidate review references an unknown or duplicate step")
        classification = str(item.get("classification") or "")
        if classification not in CANDIDATE_CLASSIFICATIONS:
            raise ValueError("Unknown candidate classification: %s" % classification)
        decision = str(item.get("decision") or "")
        expected_decision = (
            "accept" if classification == "pivotal_root_cause" else "reject"
        )
        if decision != expected_decision:
            raise ValueError("candidate decision conflicts with its classification")
        evidence = {}
        evidence_valid = True
        for name in ("supporting_evidence", "counter_evidence"):
            values = item.get(name) or []
            if not isinstance(values, list):
                raise ValueError("%s must be a list" % name)
            for value in values:
                if not isinstance(value, dict) or value.get("step_id") not in by_id:
                    raise ValueError("candidate evidence references an unknown step")
                quote = _normalized_text(value.get("quote"))
                if not quote or quote not in by_id[value["step_id"]]["content"]:
                    evidence_valid = False
            evidence[name] = values
        if decision == "accept" and not evidence["supporting_evidence"]:
            raise ValueError("accepted candidate requires supporting evidence")
        reviews.append(
            {
                "step_id": step_id,
                "classification": classification,
                "decision": decision,
                **evidence,
                "evidence_valid": evidence_valid,
                "reason": str(item.get("reason") or ""),
            }
        )
        seen.add(step_id)
    if seen != expected:
        raise ValueError("candidate_reviews must review every candidate exactly once")
    return reviews


def _validate_final(
    result: Dict[str, Any],
    steps: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    valid_steps = {step["step_index"] for step in steps}
    by_id = {
        item["step_index"]: {
            "content": _normalized_text(item.get("content")),
            "actor": str(item.get("actor") or item.get("role") or "unknown"),
        }
        for item in steps
    }
    candidate_reviews = _candidate_reviews(result, candidates, by_id)
    if not isinstance(result.get("attributable"), bool):
        raise ValueError("attributable must be boolean")
    confidence = float(result.get("confidence") or 0)
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between zero and one")
    if not bool(result.get("attributable")):
        return {
            "attributable": False,
            "first_error_step": None,
            "responsible_actor": None,
            "responsibility_domain": "unknown_mixed",
            "failure_mode": "unknown",
            "summary": str(result.get("summary") or "Insufficient evidence"),
            "supporting_evidence": [],
            "counter_evidence": result.get("counter_evidence") or [],
            "candidate_reviews": candidate_reviews,
            "causal_links": [],
            "evidence_step_ids": [],
            "confidence": confidence,
            "candidate_miss": False,
            "evidence_valid": False,
        }
    step_id = result.get("first_error_step")
    if not isinstance(step_id, int) or step_id not in valid_steps:
        raise ValueError("first_error_step must reference a real step")
    domain = str(result.get("responsibility_domain") or "unknown_mixed")
    if domain not in DOMAINS:
        raise ValueError("Unknown responsibility_domain: %s" % domain)
    failure_mode = str(result.get("failure_mode") or "unknown")
    if failure_mode not in FAILURE_MODES:
        raise ValueError("Unknown failure_mode: %s" % failure_mode)
    review_by_step = {item["step_id"]: item for item in candidate_reviews}
    accepted = sorted(
        item["step_id"] for item in candidate_reviews if item["decision"] == "accept"
    )
    if accepted and step_id != accepted[0]:
        raise ValueError("first_error_step must be the earliest accepted candidate")
    if not accepted and step_id in review_by_step:
        raise ValueError("A rejected candidate cannot be selected as the root cause")
    supporting = result.get("supporting_evidence") or []
    if not isinstance(supporting, list):
        raise ValueError("supporting_evidence must be a list")
    evidence_valid = bool(supporting) and all(
        item["evidence_valid"] for item in candidate_reviews
    )
    evidence_ids = []
    for item in supporting:
        if not isinstance(item, dict) or item.get("step_id") not in valid_steps:
            evidence_valid = False
            continue
        evidence_ids.append(item["step_id"])
        quote = _normalized_text(item.get("quote"))
        if not quote or quote not in by_id[item["step_id"]]["content"]:
            evidence_valid = False
    causal_links = result.get("causal_links") or []
    if not isinstance(causal_links, list):
        raise ValueError("causal_links must be a list")
    for link in causal_links:
        if not isinstance(link, dict):
            raise ValueError("causal link must be an object")
        if link.get("from_step") not in valid_steps:
            raise ValueError("causal link references an unknown source step")
        to_step = link.get("to_step")
        if to_step != "outcome" and to_step not in valid_steps:
            raise ValueError("causal link references an unknown target step")
    return {
        "attributable": True,
        "first_error_step": step_id,
        "responsible_actor": by_id[step_id]["actor"],
        "responsibility_domain": domain,
        "failure_mode": failure_mode,
        "summary": str(result.get("summary") or ""),
        "supporting_evidence": supporting,
        "counter_evidence": result.get("counter_evidence") or [],
        "candidate_reviews": candidate_reviews,
        "causal_links": causal_links,
        "evidence_step_ids": sorted(set(evidence_ids)),
        "confidence": confidence,
        "candidate_miss": step_id not in {item["step_id"] for item in candidates},
        "evidence_valid": evidence_valid,
    }


def run_attribution_case(
    case_path: Path,
    method: str,
    model: str = "deepseek-v4-pro",
    client: Any = None,
    max_chars: int = 24000,
    checkpoint_path: Optional[Path] = None,
) -> Dict[str, Any]:
    if method not in ("raw", "graph"):
        raise ValueError("method must be raw or graph")
    case = json.loads(case_path.read_text(encoding="utf-8"))
    steps = normalize_trajectory(case)
    actors = {
        int(step["step_id"]): str(step.get("actor") or step.get("source") or "unknown")
        for step in case.get("steps", [])
        if step.get("step_id") is not None
    }
    for step in steps:
        step["actor"] = actors.get(step["step_index"], step.get("role") or "unknown")
    if not steps:
        raise ValueError("Cannot attribute an empty trajectory")
    graph = build_attribution_graph(case)
    if client is None:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required")
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    global_view: Any = (
        _timeline(steps, max_chars)
        if method == "raw"
        else _graph_view(graph, max_chars)
    )
    shared = {
        "case_id": case.get("session_id") or case_path.stem,
        "task": _clean(case.get("task"), 6000),
        "system_prompt": _clean(case.get("system_prompt"), 4000),
        "outcome": case.get("outcome") or {"status": "FAIL"},
    }
    checkpoint_key = {
        "case_sha256": hashlib.sha256(case_path.read_bytes()).hexdigest(),
        "method": method,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "judge_config": _judge_config(),
        "max_chars": max_chars,
    }
    checkpoint: Dict[str, Any] = {}
    if checkpoint_path and checkpoint_path.exists():
        saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if saved.get("run") == checkpoint_key:
            checkpoint = saved
    if checkpoint.get("candidate"):
        candidate_result = checkpoint["candidate"]["result"]
        candidate_usage = checkpoint["candidate"]["usage"]
    else:
        candidate_result, candidate_usage = _judge_call(
            client,
            model,
            CANDIDATE_PROMPT,
            {**shared, "global_view": global_view},
            CANDIDATE_CALL_CONFIG,
        )
    candidates = _candidates(candidate_result, {step["step_index"] for step in steps})
    if checkpoint_path and not checkpoint.get("candidate"):
        checkpoint = {
            "run": checkpoint_key,
            "candidate": {"result": candidate_result, "usage": candidate_usage},
        }
        _write_json(checkpoint_path, checkpoint)
    evidence = _evidence_packets(steps, candidates, method, graph, max_chars // 2)
    if checkpoint.get("verify"):
        final_result = checkpoint["verify"]["result"]
        verify_usage = checkpoint["verify"]["usage"]
    else:
        final_result, verify_usage = _judge_call(
            client,
            model,
            VERIFY_PROMPT,
            {
                **shared,
                "failure_modes": sorted(FAILURE_MODES),
                "global_view": global_view,
                "candidates": candidates,
                "evidence_packets": evidence,
            },
            VERIFY_CALL_CONFIG,
        )
        if checkpoint_path:
            checkpoint["verify"] = {
                "result": final_result,
                "usage": verify_usage,
            }
            _write_json(checkpoint_path, checkpoint)
    attribution = _validate_final(final_result, steps, candidates)
    return {
        "case_id": shared["case_id"],
        "case_sha256": checkpoint_key["case_sha256"],
        "method": method,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "judge_config": _judge_config(),
        "max_chars": max_chars,
        "candidates": candidates,
        "attribution": attribution,
        "usage": {
            "calls": 2,
            "input_tokens": candidate_usage["input_tokens"]
            + verify_usage["input_tokens"],
            "output_tokens": candidate_usage["output_tokens"]
            + verify_usage["output_tokens"],
            "total_tokens": candidate_usage["total_tokens"]
            + verify_usage["total_tokens"],
            "latency_seconds": candidate_usage["latency_seconds"]
            + verify_usage["latency_seconds"],
            "per_call": [candidate_usage, verify_usage],
        },
    }


def run_attribution_directory(
    cases_dir: Path,
    output_dir: Path,
    method: str,
    model: str,
    limit: int = 0,
    force: bool = False,
    max_chars: int = 24000,
    split: Optional[str] = None,
) -> int:
    count = 0
    for case_path in sorted(cases_dir.glob("*.json")):
        if split:
            case = json.loads(case_path.read_text(encoding="utf-8"))
            if (case.get("provenance") or {}).get("split") != split:
                continue
        output_path = output_dir / method / case_path.name
        checkpoint_path = output_dir / ".checkpoints" / method / case_path.name
        if output_path.exists() and not force:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
            if (
                existing.get("prompt_version") != PROMPT_VERSION
                or existing.get("judge_config") != _judge_config()
                or existing.get("case_sha256")
                != hashlib.sha256(case_path.read_bytes()).hexdigest()
            ):
                raise ValueError(
                    "%s uses different Judge prompts or settings; "
                    "use --force or a new output directory" % output_path
                )
            continue
        if force:
            checkpoint_path.unlink(missing_ok=True)
        result = run_attribution_case(
            case_path,
            method,
            model,
            max_chars=max_chars,
            checkpoint_path=checkpoint_path,
        )
        _write_json(output_path, result)
        checkpoint_path.unlink(missing_ok=True)
        count += 1
        if limit and count >= limit:
            break
    return count


def _method_metrics(
    results_dir: Path, labels_dir: Path, split: Optional[str] = None
) -> Dict[str, Any]:
    rows = []
    for result_path in sorted(results_dir.glob("*.json")):
        label_path = labels_dir / result_path.name
        if not label_path.exists():
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        label = json.loads(label_path.read_text(encoding="utf-8"))
        if split and label.get("split") != split:
            continue
        gold = int(label["first_error_step"])
        attribution = result["attribution"]
        predicted = attribution.get("first_error_step")
        candidate_ids = {item["step_id"] for item in result.get("candidates", [])}
        gold_actor = _normalized_text(label.get("mistake_agent"))
        rows.append(
            {
                "gold": gold,
                "predicted": predicted,
                "actor_hit": bool(
                    gold_actor
                    and gold_actor
                    in _normalized_text(attribution.get("responsible_actor"))
                ),
                "actor_evaluated": bool(label.get("actor_evaluated", True)),
                "candidate_hit": gold in candidate_ids,
                "attributable": bool(attribution.get("attributable")),
                "evidence_valid": bool(attribution.get("evidence_valid")),
                "usage": result.get("usage") or {},
            }
        )
    if not rows:
        raise ValueError("No matching result and label files in %s" % results_dir)
    total = len(rows)
    attributable = [row for row in rows if row["attributable"]]
    actor_rows = [row for row in rows if row["actor_evaluated"]]
    exact = sum(row["predicted"] == row["gold"] for row in rows)
    near = sum(
        row["predicted"] is not None and abs(row["predicted"] - row["gold"]) <= 1
        for row in rows
    )
    return {
        "cases": total,
        "candidate_top3_recall": sum(row["candidate_hit"] for row in rows) / total,
        "responsible_actor_accuracy": (
            sum(row["actor_hit"] for row in actor_rows) / len(actor_rows)
            if actor_rows
            else None
        ),
        "exact_step_accuracy": exact / total,
        "near_step_accuracy": near / total,
        "attribution_coverage": len(attributable) / total,
        "evidence_valid_rate": sum(row["evidence_valid"] for row in rows) / total,
        "input_tokens": sum(int(row["usage"].get("input_tokens") or 0) for row in rows),
        "output_tokens": sum(
            int(row["usage"].get("output_tokens") or 0) for row in rows
        ),
        "latency_seconds": sum(
            float(row["usage"].get("latency_seconds") or 0) for row in rows
        ),
    }


def compare_attribution_runs(
    raw_dir: Path,
    graph_dir: Path,
    labels_dir: Path,
    split: Optional[str] = None,
) -> Dict[str, Any]:
    def names(directory: Path) -> set:
        selected = set()
        for path in directory.glob("*.json"):
            label_path = labels_dir / path.name
            if not label_path.exists():
                continue
            label = json.loads(label_path.read_text(encoding="utf-8"))
            if not split or label.get("split") == split:
                selected.add(path.name)
        return selected

    raw_names = names(raw_dir)
    graph_names = names(graph_dir)
    if raw_names != graph_names:
        raise ValueError(
            "Raw and Graph results must contain the same cases "
            "(raw-only=%s, graph-only=%s)"
            % (len(raw_names - graph_names), len(graph_names - raw_names))
        )
    fields = ("model", "prompt_version", "max_chars", "judge_config")
    for name in raw_names:
        raw = json.loads((raw_dir / name).read_text(encoding="utf-8"))
        graph = json.loads((graph_dir / name).read_text(encoding="utf-8"))
        if any(raw.get(field) != graph.get(field) for field in fields):
            raise ValueError(
                "Raw and Graph results must use the same Judge configuration"
            )
    raw = _method_metrics(raw_dir, labels_dir, split)
    graph = _method_metrics(graph_dir, labels_dir, split)
    return {
        "split": split or "all",
        "raw": raw,
        "graph": graph,
        "comparison": {
            "exact_step_accuracy_delta": graph["exact_step_accuracy"]
            - raw["exact_step_accuracy"],
            "near_step_accuracy_delta": graph["near_step_accuracy"]
            - raw["near_step_accuracy"],
            "input_token_reduction_rate": (
                (raw["input_tokens"] - graph["input_tokens"]) / raw["input_tokens"]
                if raw["input_tokens"]
                else None
            ),
        },
    }


def _raw_view(steps: List[Dict[str, Any]], max_chars: int) -> List[Dict[str, Any]]:
    per_step = max(1600, max_chars // max(1, len(steps)))
    return [
        {
            "step_id": step["step_index"],
            "actor": step.get("actor") or step.get("role"),
            "role": step.get("role"),
            "action": step.get("action_type"),
            "content": _clean(step.get("content"), per_step),
        }
        for step in steps
    ]


def _universal_candidates(steps: List[Dict[str, Any]]) -> List[int]:
    """High-recall candidate set: every agent step, with no fixed top-k cutoff."""
    return [step["step_index"] for step in steps if step.get("role") != "tool"]


def _ollama_call(
    model: str, payload: Dict[str, Any], base_url: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    started = time.monotonic()
    body = json.dumps(
        {
            "model": model,
            "stream": False,
            "think": OLLAMA_CONFIG["thinking"],
            "format": "json",
            "messages": [
                {
                    "role": "system",
                    "content": UNIVERSAL_PROMPT.read_text(encoding="utf-8"),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "options": {
                "temperature": OLLAMA_CONFIG["temperature"],
                "seed": OLLAMA_CONFIG["seed"],
                "num_ctx": OLLAMA_CONFIG["num_ctx"],
                "num_predict": OLLAMA_CONFIG["max_tokens"],
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        envelope = json.loads(response.read().decode("utf-8"))
    content = ((envelope.get("message") or {}).get("content") or "").strip()
    if not content:
        raise ValueError("Local Judge returned an empty response")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("Local Judge response must be a JSON object")
    input_tokens = int(envelope.get("prompt_eval_count") or 0)
    output_tokens = int(envelope.get("eval_count") or 0)
    return result, {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "latency_seconds": time.monotonic() - started,
        "finish_reason": envelope.get("done_reason"),
    }


def _deepseek_call(
    model: str, payload: Dict[str, Any], api_key: str, base_url: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    started = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": UNIVERSAL_PROMPT.read_text(encoding="utf-8")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=OLLAMA_CONFIG["max_tokens"],
        extra_body={"thinking": {"type": "disabled"}},
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("DeepSeek Judge returned an empty response")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("DeepSeek Judge response must be a JSON object")
    return result, _usage(response, time.monotonic() - started)


def _validate_universal(
    result: Dict[str, Any], steps: List[Dict[str, Any]], candidates: List[int]
) -> Dict[str, Any]:
    valid_steps = {step["step_index"] for step in steps}
    reviews = result.get("candidate_reviews")
    if not isinstance(reviews, list):
        raise ValueError("candidate_reviews must be a list")
    reviewed = []
    seen = set()
    for review in reviews:
        if not isinstance(review, dict) or review.get("step_id") not in candidates:
            raise ValueError("candidate review references an unknown step")
        step_id = review["step_id"]
        if step_id in seen:
            raise ValueError("candidate review references a duplicate step")
        status = str(review.get("status") or "").upper()
        roles = [str(role).upper() for role in review.get("causal_roles") or []]
        if status not in {"SUPPORTED", "REJECTED", "UNCERTAIN"}:
            raise ValueError("unknown candidate status: %s" % status)
        if not set(roles) <= {
            "FIRST_CAUSAL_ERROR",
            "DECISIVE_FAILURE",
            "PROPAGATION",
            "SYMPTOM",
            "REPAIR",
            "IRRELEVANT",
        }:
            raise ValueError("unknown causal role")
        reviewed.append({**review, "status": status, "causal_roles": roles})
        seen.add(step_id)
    if seen != set(candidates):
        raise ValueError("Judge must review every candidate exactly once")
    attributable = bool(result.get("attributable"))
    decisive = result.get("decisive_failure_step") if attributable else None
    first = result.get("first_causal_error_step") if attributable else None
    evidence_ids = result.get("primary_evidence_step_ids") or []
    if not isinstance(evidence_ids, list) or any(item not in valid_steps for item in evidence_ids):
        raise ValueError("primary evidence references an unknown step")
    if not attributable:
        return {
            **result,
            "attributable": False,
            "first_causal_error_step": None,
            "decisive_failure_step": None,
            "failure_symptom_step": None,
            "candidate_reviews": reviewed,
            "primary_evidence_step_ids": sorted(set(evidence_ids)),
            "evidence_valid": False,
            "confidence": max(0.0, min(1.0, float(result.get("confidence") or 0))),
        }
    if decisive not in valid_steps or first not in valid_steps:
        raise ValueError("attributable result must select real causal steps")
    selected = next(item for item in reviewed if item["step_id"] == decisive)
    if selected["status"] != "SUPPORTED" or "DECISIVE_FAILURE" not in selected["causal_roles"]:
        raise ValueError("decisive step must be a supported decisive candidate")
    return {
        **result,
        "attributable": attributable,
        "first_causal_error_step": first,
        "decisive_failure_step": decisive,
        "candidate_reviews": reviewed,
        "primary_evidence_step_ids": sorted(set(evidence_ids)),
        "evidence_valid": bool(evidence_ids),
        "confidence": max(0.0, min(1.0, float(result.get("confidence") or 0))),
    }


def run_universal_attribution_case(
    case_path: Path,
    method: str,
    model: str = "qwen3.5:9b",
    max_chars: int = 70000,
    ollama_url: str = "http://127.0.0.1:11434",
    provider: str = "ollama",
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    if method not in UNIVERSAL_METHODS:
        raise ValueError("method must be one of %s" % ", ".join(UNIVERSAL_METHODS))
    case = json.loads(case_path.read_text(encoding="utf-8"))
    steps = normalize_trajectory(case)
    actors = {
        int(step["step_id"]): str(step.get("actor") or step.get("source") or "unknown")
        for step in case.get("steps", [])
        if step.get("step_id") is not None
    }
    for step in steps:
        step["actor"] = actors.get(step["step_index"], step.get("role") or "unknown")
    if not steps:
        raise ValueError("Cannot attribute an empty trajectory")
    graph = build_attribution_graph(case)
    candidates = _universal_candidates(steps)
    shared = {
        "case_id": case.get("session_id") or case_path.stem,
        "task": _clean(case.get("task"), 6000),
        "system_prompt": _clean(case.get("system_prompt"), 4000),
        "outcome": case.get("outcome") or {"status": "FAIL"},
        "candidate_step_ids": candidates,
    }
    if method == "raw_direct":
        evidence = {"raw_trajectory": _raw_view(steps, max_chars)}
    elif method == "graph_attribution":
        evidence = {"attribution_graph": _graph_view(graph, max_chars)}
    else:
        evidence = {
            "attribution_graph": _graph_view(graph, max_chars // 2),
            "raw_evidence_packets": _raw_view(steps, max_chars // 2),
        }
    payload = {**shared, "evidence_mode": method, **evidence}
    if provider == "ollama":
        raw_result, usage = _ollama_call(model, payload, ollama_url)
    elif provider == "deepseek":
        key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise ValueError("DEEPSEEK_API_KEY is required")
        raw_result, usage = _deepseek_call(
            model, payload, key, os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        )
    else:
        raise ValueError("provider must be ollama or deepseek")
    try:
        attribution = _validate_universal(raw_result, steps, candidates)
    except ValueError as error:
        error.raw_result = raw_result
        raise
    config = {
        **(
            OLLAMA_CONFIG
            if provider == "ollama"
            else {
                "temperature": 0,
                "thinking": {"type": "disabled"},
                "max_tokens": OLLAMA_CONFIG["max_tokens"],
            }
        ),
        "provider": provider,
        "base_url": (
            ollama_url
            if provider == "ollama"
            else os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        ),
        "prompt_file": UNIVERSAL_PROMPT.name,
        "prompt_sha256": hashlib.sha256(UNIVERSAL_PROMPT.read_bytes()).hexdigest(),
    }
    return {
        "case_id": shared["case_id"],
        "case_sha256": hashlib.sha256(case_path.read_bytes()).hexdigest(),
        "method": method,
        "model": model,
        "prompt_version": UNIVERSAL_PROMPT_VERSION,
        "judge_config": config,
        "max_chars": max_chars,
        "candidates": [{"step_id": step_id} for step_id in candidates],
        "candidate_set_sha256": hashlib.sha256(
            json.dumps(candidates).encode("utf-8")
        ).hexdigest(),
        "attribution": attribution,
        "usage": {"calls": 1, **usage, "per_call": [usage]},
    }


def _universal_metrics(results_dir: Path, labels_dir: Path) -> Dict[str, Any]:
    rows = []
    for result_path in sorted(results_dir.glob("*.json")):
        label_path = labels_dir / result_path.name
        if not label_path.exists():
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        label = json.loads(label_path.read_text(encoding="utf-8"))
        gold = int(label["first_error_step"])
        attribution = result["attribution"]
        predicted = attribution.get("first_causal_error_step")
        decisive = attribution.get("decisive_failure_step")
        rows.append(
            {
                "case_id": result["case_id"],
                "gold_step": gold,
                "predicted_step": predicted,
                "decisive_failure_step": decisive,
                "exact": predicted == gold,
                "near": predicted is not None and abs(predicted - gold) <= 1,
                "decisive_equals_gold": decisive == gold,
                "candidate_hit": gold in {item["step_id"] for item in result["candidates"]},
                "attributable": bool(attribution.get("attributable")),
                "evidence_valid": bool(attribution.get("evidence_valid")),
                "confidence": attribution.get("confidence"),
                "usage": result["usage"],
            }
        )
    if not rows:
        raise ValueError("No matching results and labels")
    total = len(rows)
    return {
        "cases": total,
        "exact_step_accuracy": sum(row["exact"] for row in rows) / total,
        "near_step_accuracy": sum(row["near"] for row in rows) / total,
        "decisive_step_equal_gold_rate": sum(row["decisive_equals_gold"] for row in rows) / total,
        "candidate_recall": sum(row["candidate_hit"] for row in rows) / total,
        "attribution_coverage": sum(row["attributable"] for row in rows) / total,
        "evidence_valid_rate": sum(row["evidence_valid"] for row in rows) / total,
        "input_tokens": sum(row["usage"]["input_tokens"] for row in rows),
        "output_tokens": sum(row["usage"]["output_tokens"] for row in rows),
        "total_tokens": sum(row["usage"]["total_tokens"] for row in rows),
        "latency_seconds": sum(row["usage"]["latency_seconds"] for row in rows),
        "per_case": rows,
    }


def run_universal_pilot(
    cases_dir: Path,
    labels_dir: Path,
    output_dir: Path,
    model: str = "qwen3.5:9b",
    limit: int = 5,
    split: str = "dev",
    max_chars: int = 70000,
    ollama_url: str = "http://127.0.0.1:11434",
    provider: str = "ollama",
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    selected = []
    for case_path in sorted(cases_dir.glob("*.json")):
        case = json.loads(case_path.read_text(encoding="utf-8"))
        if (case.get("provenance") or {}).get("split") == split:
            selected.append(case_path)
        if len(selected) == limit:
            break
    if len(selected) != limit:
        raise ValueError("Expected %s %s cases, found %s" % (limit, split, len(selected)))
    for method in UNIVERSAL_METHODS:
        for index, case_path in enumerate(selected, start=1):
            output_path = output_dir / method / case_path.name
            if output_path.exists():
                result = json.loads(output_path.read_text(encoding="utf-8"))
                if (
                    result.get("case_sha256")
                    != hashlib.sha256(case_path.read_bytes()).hexdigest()
                    or result.get("model") != model
                    or result.get("prompt_version") != UNIVERSAL_PROMPT_VERSION
                    or (result.get("judge_config") or {}).get("provider") != provider
                ):
                    raise ValueError("Existing result uses a different case or Judge: %s" % output_path)
                print("[%s/%s] cached %s %s" % (index, limit, method, case_path.stem), flush=True)
                continue
            try:
                result = run_universal_attribution_case(
                    case_path, method, model, max_chars, ollama_url, provider, api_key
                )
            except ValueError as error:
                if hasattr(error, "raw_result"):
                    _write_json(output_path.with_suffix(".invalid.json"), error.raw_result)
                raise
            _write_json(output_path, result)
            print("[%s/%s] %s %s" % (index, limit, method, case_path.stem), flush=True)
    metrics = {
        method: _universal_metrics(output_dir / method, labels_dir)
        for method in UNIVERSAL_METHODS
    }
    raw_tokens = metrics["raw_direct"]["total_tokens"]
    report = {
        "model": model,
        "provider": provider,
        "prompt_version": UNIVERSAL_PROMPT_VERSION,
        "split": split,
        "case_files": [path.name for path in selected],
        "methods": metrics,
        "relative_to_raw": {
            method: {
                "total_token_reduction_rate": (
                    (raw_tokens - metrics[method]["total_tokens"]) / raw_tokens
                    if raw_tokens
                    else None
                ),
                "exact_accuracy_delta": metrics[method]["exact_step_accuracy"]
                - metrics["raw_direct"]["exact_step_accuracy"],
            }
            for method in UNIVERSAL_METHODS[1:]
        },
    }
    _write_json(output_dir / "report.json", report)
    return report
