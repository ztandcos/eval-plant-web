"""Convert the public human-annotated RootSE benchmark to EvalPlant input."""

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

GOLD_LABELS_PATH = Path(__file__).with_name("rootse_gold_labels.json")
CATEGORY_NAMES = {
    "H-E": "执行环境",
    "H-T": "工具链路",
    "H-C": "上下文管理",
    "H-L": "运行生命周期",
    "H-O": "可观测性",
    "H-V": "验证与判分",
    "H-G": "治理与限制",
    "L1": "目标理解与规划",
    "L2": "推理与决策",
    "L3": "行动与工具使用",
    "L4": "反馈、验证与结束",
}


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def verifier_log(value: Any) -> str:
    if not isinstance(value, dict):
        return text(value)
    sections = []
    for name, detail in value.items():
        if isinstance(detail, dict):
            sections.append(
                "\n".join(
                    part
                    for part in (
                        "TEST: %s" % name,
                        "STATUS: %s" % text(detail.get("status")),
                        text(detail.get("error_description")),
                        text(detail.get("code")),
                    )
                    if part
                )
            )
        else:
            sections.append("TEST: %s\n%s" % (name, text(detail)))
    return "\n\n".join(sections)


def action_call(action: Any, call_id: str) -> Optional[Dict[str, Any]]:
    if not action:
        return None
    if isinstance(action, dict):
        return {
            "tool_call_id": call_id,
            "function_name": str(action.get("tool") or "rootse_action"),
            "arguments": action.get("input") or action,
        }
    if isinstance(action, list):
        return {
            "tool_call_id": call_id,
            "function_name": "rootse_batch_action",
            "arguments": {"actions": action},
        }
    return {
        "tool_call_id": call_id,
        "function_name": "shell",
        "arguments": {"command": text(action)},
    }


def case_id(data: Dict[str, Any]) -> str:
    raw = "%s__%s__%s" % (data["agent"], data["model"], data["instance_id"])
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)


def gold_key(data: Dict[str, Any]) -> str:
    return "%s|%s|%s" % (data["agent"], data["model"], data["instance_id"])


def load_gold_labels() -> Dict[str, Dict[str, str]]:
    payload = json.loads(GOLD_LABELS_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("RootSE gold labels must be a JSON object")
    return payload


def mapped_gold(data: Dict[str, Any], labels: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    key = gold_key(data)
    mapped = labels.get(key)
    if not mapped:
        raise ValueError("Missing EvalPlat gold mapping for %s" % key)
    responsibility = str(mapped["responsibility"])
    category_code = str(mapped["category_code"])
    expected = "HARNESS" if category_code.startswith("H-") else "LLM"
    if responsibility != expected:
        raise ValueError("Gold responsibility/category mismatch for %s" % key)
    if category_code not in CATEGORY_NAMES:
        raise ValueError("Unknown gold category %s for %s" % (category_code, key))
    return {
        "responsibility": responsibility,
        "category_code": category_code,
        "category_name": CATEGORY_NAMES[category_code],
        "notes": str(mapped.get("notes") or ""),
    }


def convert(
    data: Dict[str, Any], labels: Dict[str, Dict[str, str]]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    identifier = case_id(data)
    steps = [
        {
            "step_id": 1,
            "source": "user",
            "message": text(data["original_mission_prompt"]),
        }
    ]
    for position, source in enumerate(data["original_traj"]):
        agent_step = position * 2 + 2
        call_id = "rootse-%s" % position
        call = action_call(source.get("action"), call_id)
        message = "\n\n".join(
            part
            for part in (
                "THOUGHT:\n%s" % text(source.get("thought")),
                "RESPONSE:\n%s" % text(source.get("response")),
            )
            if not part.endswith("\n")
        )
        agent_event: Dict[str, Any] = {
            "step_id": agent_step,
            "source": "agent",
            "message": message,
            "extra": {"rootse_source_index": position},
        }
        if call:
            agent_event["tool_calls"] = [call]
        steps.append(agent_event)
        if call:
            steps.append(
                {
                    "step_id": agent_step + 1,
                    "source": "tool",
                    "observation": {
                        "results": [
                            {
                                "source_call_id": call_id,
                                "content": text(source.get("observation")),
                            }
                        ]
                    },
                }
            )
    failure_id = int(data["failure_id"])
    if not 0 <= failure_id < len(data["original_traj"]):
        raise ValueError("Invalid RootSE failure_id for %s" % identifier)
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "session_id": identifier,
        "agent": {
            "name": data["agent"],
            "version": "rootse-public",
            "model_name": data["model"],
        },
        "steps": steps,
        "extra": {
            "source_dataset": "dengdan1999/RootSE",
            "source_instance_id": data["instance_id"],
        },
    }
    mapped = mapped_gold(data, labels)
    gold = {
        "case_id": identifier,
        "responsibility": mapped["responsibility"],
        "category_code": mapped["category_code"],
        "category_name": mapped["category_name"],
        "root_cause_step": failure_id * 2 + 2,
        "source_root_cause_step": failure_id,
        "failure_reason": text(data["failure_reason"]),
        "label_source": "RootSE human annotation mapped to EvalPlat taxonomy",
        "responsibility_basis": mapped["notes"],
        "notes": mapped["notes"],
        "annotator": "RootSE human experts; taxonomy mapped by evalplat",
    }
    return trajectory, gold


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=102)
    args = parser.parse_args()
    labels = load_gold_labels()
    sources = sorted(
        path
        for path in args.source.rglob("*.json")
        if path.name != "rootse_gold_labels.json"
    )[: args.limit]
    if len(sources) < args.limit:
        raise ValueError("Requested %s cases but found %s" % (args.limit, len(sources)))
    seen_keys = set()
    args.output.mkdir(parents=True, exist_ok=True)
    gold_rows = []
    step_count = 0
    category_counts: Dict[str, int] = {}
    for source_path in sources:
        raw = source_path.read_bytes()
        source = json.loads(raw)
        seen_keys.add(gold_key(source))
        trajectory, gold = convert(source, labels)
        category_counts[gold["category_code"]] = (
            category_counts.get(gold["category_code"], 0) + 1
        )
        target = args.output / "cases" / gold["case_id"]
        (target / "agent").mkdir(parents=True, exist_ok=True)
        (target / "verifier").mkdir(exist_ok=True)
        (target / "agent" / "trajectory.json").write_text(
            json.dumps(trajectory, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        failed_tests = verifier_log(source.get("failed_tests"))
        (target / "verifier" / "test-stdout.txt").write_text(
            failed_tests + "\n", encoding="utf-8"
        )
        (target / "result.json").write_text(
            json.dumps(
                {
                    "id": hashlib.sha256(gold["case_id"].encode()).hexdigest()[:32],
                    "task_name": source["instance_id"],
                    "trial_name": gold["case_id"],
                    "agent_info": {
                        "name": source["agent"],
                        "version": "rootse-public",
                        "model_info": {"name": source["model"]},
                    },
                    "extra": {
                        "source_dataset": "dengdan1999/RootSE",
                        "source_instance_id": source["instance_id"],
                        "label_source": "RootSE human annotation",
                    },
                    "agent_result": {},
                    "verifier_result": {"rewards": {"rootse_failure": 0.0}},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (target / "source.json").write_text(
            json.dumps(
                {
                    "dataset": "dengdan1999/RootSE",
                    "license": "MIT",
                    "source_path": str(source_path),
                    "source_sha256": hashlib.sha256(raw).hexdigest(),
                    "gold_separated": True,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        gold_rows.append(gold)
        step_count += len(trajectory["steps"])
    extra = sorted(seen_keys - set(labels))
    if extra:
        raise ValueError("Gold mapping missing keys: %s" % extra[:5])
    if args.limit >= len(labels):
        missing = sorted(set(labels) - seen_keys)
        if missing:
            raise ValueError("Unused gold labels because source cases missing: %s" % missing[:5])
    (args.output / "gold.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in gold_rows),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "cases": len(gold_rows),
                "atif_steps": step_count,
                "gold": "gold.jsonl",
                "gold_categories": category_counts,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
