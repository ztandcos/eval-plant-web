import sqlite3
from typing import Any, Dict, Iterable, Optional


def _average(values: Iterable[Optional[float]]) -> Optional[float]:
    items = [float(value) for value in values if value is not None]
    return sum(items) / len(items) if items else None


def _counts(connection: sqlite3.Connection, query: str, params: tuple) -> Dict[str, int]:
    return {
        str(row["name"] or "UNKNOWN"): int(row["count"])
        for row in connection.execute(query, params).fetchall()
    }


def _grouped(connection: sqlite3.Connection, experiment_id: str, field: str) -> Dict[str, Dict[str, int]]:
    rows = connection.execute(
        """
        SELECT COALESCE(t.%s, 'UNKNOWN') name,
               COUNT(*) total,
               SUM(CASE WHEN t.verdict='PASS' THEN 1 ELSE 0 END) passed,
               SUM(CASE WHEN t.verdict!='PASS' THEN 1 ELSE 0 END) failed,
               SUM(CASE WHEN d.responsibility='HARNESS' THEN 1 ELSE 0 END) harness,
               SUM(CASE WHEN d.responsibility='LLM' THEN 1 ELSE 0 END) llm
        FROM trajectories t LEFT JOIN diagnoses d ON d.trajectory_id=t.id
        WHERE t.experiment_id=? GROUP BY COALESCE(t.%s, 'UNKNOWN') ORDER BY name
        """ % (field, field),
        (experiment_id,),
    ).fetchall()
    return {
        str(row["name"]): {
            "total": int(row["total"]),
            "passed": int(row["passed"] or 0),
            "failed": int(row["failed"] or 0),
            "harness": int(row["harness"] or 0),
            "llm": int(row["llm"] or 0),
        }
        for row in rows
    }


def report(connection: sqlite3.Connection, experiment_id: str) -> Dict[str, Any]:
    trajectories = connection.execute(
        "SELECT * FROM trajectories WHERE experiment_id=?", (experiment_id,)
    ).fetchall()
    if not trajectories:
        raise ValueError("Unknown or empty experiment: %s" % experiment_id)
    params = (experiment_id,)
    verdicts = _counts(
        connection,
        "SELECT verdict name, COUNT(*) count FROM trajectories WHERE experiment_id=? GROUP BY verdict",
        params,
    )
    diagnosis_statuses = _counts(
        connection,
        """
        SELECT d.status name, COUNT(*) count FROM diagnoses d
        JOIN trajectories t ON t.id=d.trajectory_id
        WHERE t.experiment_id=? GROUP BY d.status
        """,
        params,
    )
    responsibilities = _counts(
        connection,
        """
        SELECT d.responsibility name, COUNT(*) count FROM diagnoses d
        JOIN trajectories t ON t.id=d.trajectory_id
        WHERE t.experiment_id=? AND d.responsibility IS NOT NULL GROUP BY d.responsibility
        """,
        params,
    )
    categories = _counts(
        connection,
        """
        SELECT d.category_code name, COUNT(*) count FROM diagnoses d
        JOIN trajectories t ON t.id=d.trajectory_id
        WHERE t.experiment_id=? AND d.category_code IS NOT NULL GROUP BY d.category_code
        """,
        params,
    )
    confidence = _counts(
        connection,
        """
        SELECT d.confidence name, COUNT(*) count FROM diagnoses d
        JOIN trajectories t ON t.id=d.trajectory_id
        WHERE t.experiment_id=? AND d.confidence IS NOT NULL GROUP BY d.confidence
        """,
        params,
    )
    decision_sources = _counts(
        connection,
        """
        SELECT d.decision_source name, COUNT(*) count FROM diagnoses d
        JOIN trajectories t ON t.id=d.trajectory_id
        WHERE t.experiment_id=? AND d.decision_source IS NOT NULL GROUP BY d.decision_source
        """,
        params,
    )
    components = _counts(
        connection,
        """
        SELECT d.component name, COUNT(*) count FROM diagnoses d
        JOIN trajectories t ON t.id=d.trajectory_id
        WHERE t.experiment_id=? AND d.component IS NOT NULL GROUP BY d.component
        ORDER BY count DESC
        """,
        params,
    )
    diagnoses = connection.execute(
        """
        SELECT d.* FROM diagnoses d JOIN trajectories t ON t.id=d.trajectory_id
        WHERE t.experiment_id=?
        """,
        params,
    ).fetchall()
    return {
        "experiment": experiment_id,
        "total_tasks": len(trajectories),
        "successful_tasks": verdicts.get("PASS", 0),
        "failed_tasks": len(trajectories) - verdicts.get("PASS", 0),
        "verdicts": verdicts,
        "diagnosis_statuses": diagnosis_statuses,
        "responsibilities": responsibilities,
        "harness_layers": {key: value for key, value in categories.items() if key.startswith("H-")},
        "llm_categories": {key: value for key, value in categories.items() if key.startswith("L")},
        "confidence": confidence,
        "decision_sources": decision_sources,
        "components": components,
        "by_model": _grouped(connection, experiment_id, "model_name"),
        "by_agent_version": _grouped(connection, experiment_id, "agent_version"),
        "average_input_tokens": _average(row["input_tokens"] for row in trajectories),
        "average_cache_tokens": _average(row["cache_tokens"] for row in trajectories),
        "average_output_tokens": _average(row["output_tokens"] for row in trajectories),
        "average_cost": _average(row["cost"] for row in trajectories),
        "average_agent_seconds": _average(row["agent_execution_seconds"] for row in trajectories),
        "average_verifier_seconds": _average(row["verifier_seconds"] for row in trajectories),
        "diagnosis_input_tokens": sum(int(row["judge_input_tokens"] or 0) for row in diagnoses),
        "diagnosis_output_tokens": sum(int(row["judge_output_tokens"] or 0) for row in diagnoses),
        "diagnosis_latency_seconds": sum(float(row["judge_latency_seconds"] or 0) for row in diagnoses),
    }
