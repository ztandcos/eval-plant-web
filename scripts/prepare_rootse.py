"""Convert the public human-annotated RootSE benchmark to EvalPlant input."""

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


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


def convert(data: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
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
    gold = {
        "case_id": identifier,
        "responsibility": "LLM",
        "root_cause_step": failure_id * 2 + 2,
        "source_root_cause_step": failure_id,
        "failure_reason": text(data["failure_reason"]),
        "label_source": "RootSE human annotation",
        "responsibility_basis": (
            "RootSE labels an agent-generated trajectory step; the dataset does "
            "not provide an EvalPlant L1-L4 category."
        ),
        "annotator": "RootSE human experts",
    }
    return trajectory, gold


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=102)
    args = parser.parse_args()
    sources = sorted(args.source.rglob("*.json"))[: args.limit]
    if len(sources) < args.limit:
        raise ValueError("Requested %s cases but found %s" % (args.limit, len(sources)))
    args.output.mkdir(parents=True, exist_ok=True)
    gold_rows = []
    step_count = 0
    for source_path in sources:
        raw = source_path.read_bytes()
        source = json.loads(raw)
        trajectory, gold = convert(source)
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
    (args.output / "gold.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in gold_rows),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"cases": len(gold_rows), "atif_steps": step_count, "gold": "gold.jsonl"},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
