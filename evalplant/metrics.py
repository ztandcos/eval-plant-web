import sqlite3
from typing import Any, Dict, Optional, Sequence, Tuple


def _average(values: Sequence[Optional[float]]) -> Optional[float]:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


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
        ORDER BY base_task_id, started_at, trial_name
        """,
        (experiment_id,),
    ).fetchall()
    grouped = {}
    for row in outcome_rows:
        grouped.setdefault(row["base_task_id"], []).append(row["reward"])
    repeated = [rewards for rewards in grouped.values() if len(rewards) >= 3]
    efficiency_rows = connection.execute(
        """
        SELECT t.*, COUNT(s.step_index) step_count,
               SUM(CASE WHEN s.action_type='tool_error' THEN 1 ELSE 0 END)
                   tool_error_count
        FROM trajectories t
        LEFT JOIN steps s ON s.trajectory_id=t.id
        WHERE t.experiment_id=? AND COALESCE(t.health_status, 'VALID')='VALID'
        GROUP BY t.id
        """,
        (experiment_id,),
    ).fetchall()
    security_rows = connection.execute(
        """
        SELECT sm.metric_name, AVG(sm.value) mean, COUNT(*) samples
        FROM security_metrics sm
        JOIN trajectories t ON t.id=sm.trajectory_id
        WHERE t.experiment_id=?
        GROUP BY sm.metric_name ORDER BY sm.metric_name
        """,
        (experiment_id,),
    ).fetchall()
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
        "valid_trials": len(outcome_rows),
        "unique_tasks": len(grouped),
        "repeated_tasks": len(repeated),
        "unstable_task_rate": (
            sum(min(rewards) != max(rewards) for rewards in repeated) / len(repeated)
            if repeated
            else None
        ),
        "average_steps": _average([row["step_count"] for row in efficiency_rows]),
        "average_tool_errors": _average(
            [row["tool_error_count"] for row in efficiency_rows]
        ),
        "average_input_tokens": _average(
            [row["input_tokens"] for row in efficiency_rows]
        ),
        "average_cache_tokens": _average(
            [row["cache_tokens"] for row in efficiency_rows]
        ),
        "average_output_tokens": _average(
            [row["output_tokens"] for row in efficiency_rows]
        ),
        "average_environment_setup_seconds": _average(
            [row["environment_setup_seconds"] for row in efficiency_rows]
        ),
        "average_agent_setup_seconds": _average(
            [row["agent_setup_seconds"] for row in efficiency_rows]
        ),
        "average_agent_execution_seconds": _average(
            [row["agent_execution_seconds"] for row in efficiency_rows]
        ),
        "average_verifier_seconds": _average(
            [row["verifier_seconds"] for row in efficiency_rows]
        ),
        "security_metrics": {
            row["metric_name"]: {"mean": row["mean"], "samples": row["samples"]}
            for row in security_rows
        },
    }


def compare_experiments(
    connection: sqlite3.Connection, experiment_a: str, experiment_b: str
) -> Dict[str, Any]:
    def task_rows(experiment: str) -> Dict[str, sqlite3.Row]:
        rows = connection.execute(
            """
            SELECT t.base_task_id, COUNT(*) trials, AVG(t.reward) reward,
                   AVG(t.input_tokens) input_tokens,
                   AVG(t.cache_tokens) cache_tokens,
                   AVG(t.output_tokens) output_tokens,
                   AVG(t.agent_execution_seconds) agent_seconds,
                   AVG(sc.step_count) steps,
                   AVG(sc.tool_errors) tool_errors
            FROM trajectories t
            JOIN (
                SELECT trajectory_id, COUNT(*) step_count,
                       SUM(CASE WHEN action_type='tool_error' THEN 1 ELSE 0 END)
                           tool_errors
                FROM steps GROUP BY trajectory_id
            ) sc ON sc.trajectory_id=t.id
            WHERE t.experiment_id=?
              AND COALESCE(t.health_status, 'VALID')='VALID'
            GROUP BY t.base_task_id
            """,
            (experiment,),
        ).fetchall()
        return {row["base_task_id"]: row for row in rows}

    left = task_rows(experiment_a)
    right = task_rows(experiment_b)
    tasks = []
    for task_id in sorted(left.keys() & right.keys()):
        a, b = left[task_id], right[task_id]
        tasks.append(
            {
                "task_id": task_id,
                "reward_a": a["reward"],
                "reward_b": b["reward"],
                "input_tokens_a": a["input_tokens"],
                "input_tokens_b": b["input_tokens"],
                "output_tokens_a": a["output_tokens"],
                "output_tokens_b": b["output_tokens"],
                "agent_seconds_a": a["agent_seconds"],
                "agent_seconds_b": b["agent_seconds"],
                "steps_a": a["steps"],
                "steps_b": b["steps"],
                "tool_errors_a": a["tool_errors"],
                "tool_errors_b": b["tool_errors"],
            }
        )
    return {
        "experiment_a": experiment_a,
        "experiment_b": experiment_b,
        "tasks": tasks,
        "only_a": sorted(left.keys() - right.keys()),
        "only_b": sorted(right.keys() - left.keys()),
        "summary_a": report(connection, experiment_a, "test"),
        "summary_b": report(connection, experiment_b, "test"),
    }
