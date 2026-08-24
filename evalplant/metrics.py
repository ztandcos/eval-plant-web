import sqlite3
from typing import Any, Dict, Optional, Sequence, Tuple


def macro_f1(pairs: Sequence[Tuple[str, str]]) -> Optional[float]:
    if not pairs:
        return None
    labels = sorted({value for pair in pairs for value in pair})
    scores = []
    for label in labels:
        true_positive = sum(
            gold == label and predicted == label for gold, predicted in pairs
        )
        false_positive = sum(
            gold != label and predicted == label for gold, predicted in pairs
        )
        false_negative = sum(
            gold == label and predicted != label for gold, predicted in pairs
        )
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        )
        scores.append(
            2 * precision * recall / (precision + recall) if precision + recall else 0.0
        )
    return sum(scores) / len(scores)


def report(
    connection: sqlite3.Connection, experiment_id: str, split: str
) -> Dict[str, Any]:
    verdict_rows = connection.execute(
        """SELECT verdict, COUNT(*) count
           FROM trajectories WHERE experiment_id=? GROUP BY verdict""",
        (experiment_id,),
    ).fetchall()
    rows = connection.execute(
        """
        SELECT a.*, p.attributable, p.first_error_step predicted_step,
               p.stage predicted_stage, p.mechanism predicted_mechanism,
               p.subcategory predicted_subcategory
        FROM annotations a
        JOIN trajectories t ON t.id = a.trajectory_id
        LEFT JOIN attributions p ON p.trajectory_id = a.trajectory_id
        WHERE t.experiment_id = ? AND a.split = ?
        """,
        (experiment_id, split),
    ).fetchall()
    evaluated = [row for row in rows if row["predicted_step"] is not None]
    exact = sum(row["first_error_step"] == row["predicted_step"] for row in evaluated)
    near = sum(
        abs(row["first_error_step"] - row["predicted_step"]) <= 1 for row in evaluated
    )
    evidence = [
        row["evidence_pass"] for row in rows if row["evidence_pass"] is not None
    ]
    outcome_rows = connection.execute(
        """
        SELECT base_task_id, verdict, reward FROM trajectories
        WHERE experiment_id=? AND COALESCE(health_status, 'VALID')='VALID'
          AND reward IS NOT NULL
        """,
        (experiment_id,),
    ).fetchall()
    grouped = {}
    for row in outcome_rows:
        grouped.setdefault(row["base_task_id"], []).append(row["reward"])
    repeated = [rewards for rewards in grouped.values() if len(rewards) >= 3]
    return {
        "experiment": experiment_id,
        "split": split,
        "verdicts": {row["verdict"]: row["count"] for row in verdict_rows},
        "annotations": len(rows),
        "evaluated": len(evaluated),
        "exact_step_accuracy": exact / len(evaluated) if evaluated else None,
        "near_step_accuracy": near / len(evaluated) if evaluated else None,
        "stage_macro_f1": macro_f1(
            [
                (row["stage"], row["predicted_stage"])
                for row in evaluated
                if row["predicted_stage"]
            ]
        ),
        "mechanism_macro_f1": macro_f1(
            [
                (row["mechanism"], row["predicted_mechanism"])
                for row in evaluated
                if row["predicted_mechanism"]
            ]
        ),
        "subcategory_macro_f1": macro_f1(
            [
                (row["subcategory"], row["predicted_subcategory"])
                for row in evaluated
                if row["subcategory"] and row["predicted_subcategory"]
            ]
        ),
        "evidence_pass_rate": sum(evidence) / len(evidence) if evidence else None,
        "attribution_coverage": len(evaluated) / len(rows) if rows else None,
        "average_reward": (
            sum(row["reward"] for row in outcome_rows) / len(outcome_rows)
            if outcome_rows
            else None
        ),
        "pass_all_repeats": (
            sum(all(value == 1 for value in rewards) for rewards in repeated)
            / len(repeated)
            if repeated
            else None
        ),
        "pass_at_3": (
            sum(any(value == 1 for value in rewards[:3]) for rewards in repeated)
            / len(repeated)
            if repeated
            else None
        ),
    }
