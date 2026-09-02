"""Convert LongRCA coding traces (SWE-bench Pro + Terminal-Bench 2) to EvalPlat input."""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

GOLD_LABELS_PATH = Path(__file__).with_name("longrca_gold_labels.json")
CODING_PREFIXES = ("swe_bench_pro__", "terminal_bench_2__")
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
FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def load_gold_labels() -> Dict[str, Dict[str, str]]:
    payload = json.loads(GOLD_LABELS_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("LongRCA gold labels must be a JSON object")
    return payload


def mapped_gold(question_id: str, labels: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    mapped = labels.get(question_id)
    if not mapped:
        raise ValueError("Missing EvalPlat gold mapping for %s" % question_id)
    responsibility = str(mapped["responsibility"])
    category_code = str(mapped["category_code"])
    expected = "HARNESS" if category_code.startswith("H-") else "LLM"
    if responsibility != expected:
        raise ValueError("Gold responsibility/category mismatch for %s" % question_id)
    if category_code not in CATEGORY_NAMES:
        raise ValueError("Unknown gold category %s for %s" % (category_code, question_id))
    return {
        "responsibility": responsibility,
        "category_code": category_code,
        "category_name": CATEGORY_NAMES[category_code],
        "notes": str(mapped.get("notes") or ""),
    }


def step_source(item: Dict[str, Any]) -> str:
    name = str(item.get("name") or "").strip()
    if name == "human":
        return "user"
    if name == "Computer_terminal":
        return "tool"
    return "agent"


def last_fence(message: str) -> Optional[Tuple[str, str]]:
    match = None
    for match in FENCE_RE.finditer(message):
        pass
    if match is None:
        return None
    language = (match.group(1) or "").strip().split()[0] if match.group(1) else ""
    body = match.group(2).strip()
    if not body:
        return None
    return language.lower() or "computer_terminal", body


def tool_name(language: str) -> str:
    if language in {"bash", "sh", "shell", "zsh"}:
        return "bash"
    if language in {"view", "cat"}:
        return "view"
    if language in {"python", "py"}:
        return "python"
    return language or "computer_terminal"


def convert_history(history: List[Dict[str, Any]], question_id: str) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    for position, item in enumerate(history):
        if not isinstance(item, dict):
            raise ValueError("%s history[%s] is not an object" % (question_id, position))
        step_id = int(item["step"])
        source = step_source(item)
        message = text(item.get("content"))
        event: Dict[str, Any] = {
            "step_id": step_id,
            "source": source,
            "extra": {
                "longrca_role": item.get("role"),
                "longrca_name": item.get("name"),
            },
        }
        if source == "tool":
            call_id = "longrca-%s-%s" % (question_id, step_id)
            if steps and steps[-1].get("source") == "agent":
                previous = steps[-1]
                if not previous.get("tool_calls"):
                    fence = last_fence(str(previous.get("message") or ""))
                    language, body = fence if fence else ("computer_terminal", "")
                    previous["tool_calls"] = [
                        {
                            "tool_call_id": call_id,
                            "function_name": tool_name(language),
                            "arguments": {"command": body} if body else {"logged": True},
                        }
                    ]
                else:
                    call_id = previous["tool_calls"][0]["tool_call_id"]
            event["observation"] = {
                "results": [{"source_call_id": call_id, "content": message}]
            }
        else:
            event["message"] = message
        steps.append(event)
    if [item["step_id"] for item in steps] != list(range(len(steps))):
        raise ValueError("%s history step_id values are not 0-based consecutive" % question_id)
    return steps


def convert(
    row: Dict[str, Any], labels: Dict[str, Dict[str, str]]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    question_id = str(row["question_ID"])
    history = row.get("history") or []
    if not isinstance(history, list) or not history:
        raise ValueError("%s has an empty history" % question_id)
    mistake_step = int(row["mistake_step"])
    if not 0 <= mistake_step < len(history):
        raise ValueError("Invalid LongRCA mistake_step for %s" % question_id)
    mapped = mapped_gold(question_id, labels)
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "session_id": question_id,
        "agent": {
            "name": "longrca-coding-agent",
            "version": "longrca-bench",
            "model_name": "unknown",
        },
        "steps": convert_history(history, question_id),
        "extra": {
            "source_dataset": "CLoud5-real/longrca-bench",
            "source_instance_id": question_id,
            "coding_split": question_id.split("__", 1)[0],
        },
    }
    gold = {
        "case_id": question_id,
        "responsibility": mapped["responsibility"],
        "category_code": mapped["category_code"],
        "category_name": mapped["category_name"],
        "root_cause_step": mistake_step,
        "source_root_cause_step": mistake_step,
        "mistake_agent": text(row.get("mistake_agent")),
        "failure_reason": text(row.get("mistake_reason")),
        "label_source": "LongRCA human annotation mapped to EvalPlat taxonomy",
        "responsibility_basis": mapped["notes"],
        "notes": mapped["notes"],
        "annotator": "LongRCA human experts; taxonomy mapped by evalplat",
    }
    return trajectory, gold


def iter_coding_rows(source: Path) -> Iterable[Dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as error:
        raise SystemExit(
            "pyarrow is required. Run: uv run --with pyarrow python scripts/prepare_longrca.py ..."
        ) from error
    paths = sorted(source.glob("*.parquet")) if source.is_dir() else [source]
    if not paths:
        raise ValueError("No parquet files found in %s" % source)
    for path in paths:
        table = pq.read_table(path)
        columns = {
            name: table.column(name).to_pylist() for name in table.column_names
        }
        for index in range(table.num_rows):
            question_id = str(columns["question_ID"][index])
            if not question_id.startswith(CODING_PREFIXES):
                continue
            yield {
                "question_ID": question_id,
                "history": columns["history"][index],
                "mistake_agent": columns["mistake_agent"][index],
                "mistake_step": columns["mistake_step"][index],
                "mistake_reason": columns["mistake_reason"][index],
                "source_path": str(path),
            }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    labels = load_gold_labels()
    args.output.mkdir(parents=True, exist_ok=True)
    gold_rows = []
    seen = set()
    step_count = 0
    category_counts: Counter[str] = Counter()
    for row in iter_coding_rows(args.source):
        question_id = row["question_ID"]
        if question_id in seen:
            raise ValueError("Duplicate LongRCA question_ID %s" % question_id)
        seen.add(question_id)
        trajectory, gold = convert(row, labels)
        category_counts[gold["category_code"]] += 1
        target = args.output / "cases" / gold["case_id"]
        (target / "agent").mkdir(parents=True, exist_ok=True)
        (target / "verifier").mkdir(exist_ok=True)
        (target / "agent" / "trajectory.json").write_text(
            json.dumps(trajectory, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (target / "verifier" / "test-stdout.txt").write_text(
            "Official evaluator returned reward=0 (FAIL).\n",
            encoding="utf-8",
        )
        (target / "result.json").write_text(
            json.dumps(
                {
                    "id": hashlib.sha256(gold["case_id"].encode()).hexdigest()[:32],
                    "task_name": gold["case_id"],
                    "trial_name": gold["case_id"],
                    "agent_info": {
                        "name": "longrca-coding-agent",
                        "version": "longrca-bench",
                        "model_info": {"name": "unknown"},
                    },
                    "extra": {
                        "source_dataset": "CLoud5-real/longrca-bench",
                        "source_instance_id": gold["case_id"],
                        "label_source": "LongRCA human annotation",
                        "coding_split": gold["case_id"].split("__", 1)[0],
                    },
                    "agent_result": {},
                    "verifier_result": {"rewards": {"longrca_failure": 0.0}},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        gold_rows.append(gold)
        step_count += len(trajectory["steps"])
        if args.limit and len(gold_rows) >= args.limit:
            break
    extra = sorted(seen - set(labels))
    if extra:
        raise ValueError("Gold mapping missing keys: %s" % extra[:5])
    if not args.limit:
        missing = sorted(set(labels) - seen)
        if missing:
            raise ValueError(
                "Unused gold labels because source cases missing: %s" % missing[:5]
            )
    gold_rows.sort(key=lambda row: row["case_id"])
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
                "gold_categories": dict(category_counts),
                "splits": dict(Counter(row["case_id"].split("__", 1)[0] for row in gold_rows)),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
