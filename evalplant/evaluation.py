import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_predictions(path: Path) -> Dict[str, Dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("diagnoses") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Predictions must be a report export or JSON array")
    return {
        str(
            row.get("case_id") or row.get("task_id") or row.get("trajectory_id")
        ): row.get("diagnosis", row)
        for row in rows
    }


def _rate(matches: Iterable[bool]) -> float:
    values = list(matches)
    return sum(values) / len(values) if values else 0.0


def evaluate(
    gold: List[Dict[str, Any]],
    predictions: Dict[str, Dict[str, Any]],
    reviews: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    paired = [
        (row, predictions[str(row["case_id"])])
        for row in gold
        if str(row["case_id"]) in predictions
    ]
    attributed = [
        (expected, actual)
        for expected, actual in paired
        if actual.get("status") == "ATTRIBUTED"
    ]
    fields = {
        "responsibility": "responsibility",
        "category": "category_code",
        "root_step_exact": "root_cause_step",
    }
    result = {
        "gold_cases": len(gold),
        "paired_cases": len(paired),
        "coverage": len(attributed) / len(paired) if paired else 0.0,
        "root_step_near_accuracy": _rate(
            actual.get("status") == "ATTRIBUTED"
            and isinstance(actual.get("root_cause_step"), int)
            and isinstance(expected.get("root_cause_step"), int)
            and abs(actual["root_cause_step"] - expected["root_cause_step"]) <= 1
            for expected, actual in paired
        ),
        "selective_root_step_near_accuracy": _rate(
            isinstance(actual.get("root_cause_step"), int)
            and isinstance(expected.get("root_cause_step"), int)
            and abs(actual["root_cause_step"] - expected["root_cause_step"]) <= 1
            for expected, actual in attributed
        ),
        "abstentions": len(paired) - len(attributed),
    }
    for label, field in fields.items():
        result["%s_accuracy" % label] = _rate(
            actual.get("status") == "ATTRIBUTED"
            and actual.get(field) == expected.get(field)
            for expected, actual in paired
        )
        result["selective_%s_accuracy" % label] = _rate(
            actual.get(field) == expected.get(field) for expected, actual in attributed
        )
    reviewed = [
        row["evidence_supported"]
        for row in reviews or []
        if isinstance(row.get("evidence_supported"), bool)
        and str(row.get("case_id")) in predictions
    ]
    result["evidence_support_rate"] = _rate(reviewed) if reviewed else None
    result["evidence_reviews"] = len(reviewed)
    return result


def stability(prediction_sets: List[Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    if len(prediction_sets) < 2:
        return {"shared_cases": 0, "exact_agreement": 1.0}
    shared = set.intersection(*(set(items) for items in prediction_sets))
    signatures = []
    for case_id in shared:
        signatures.append(
            {
                (
                    items[case_id].get("status"),
                    items[case_id].get("responsibility"),
                    items[case_id].get("category_code"),
                    items[case_id].get("root_cause_step"),
                )
                for items in prediction_sets
            }
        )
    return {
        "shared_cases": len(shared),
        "exact_agreement": _rate(len(items) == 1 for items in signatures),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate reviewed EvalPlant diagnoses"
    )
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, nargs="+", required=True)
    parser.add_argument(
        "--reviews", type=Path, help="Optional human evidence reviews JSONL"
    )
    args = parser.parse_args()
    gold = read_jsonl(args.gold)
    prediction_sets = [read_predictions(path) for path in args.predictions]
    reviews = read_jsonl(args.reviews) if args.reviews else None
    result = evaluate(gold, prediction_sets[0], reviews)
    result["stability"] = stability(prediction_sets)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
