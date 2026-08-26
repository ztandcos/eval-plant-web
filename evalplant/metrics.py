import sqlite3
from typing import Any, Dict, Iterable, Optional

ACTION_MAPPING = {
    "H-E": "检查镜像、依赖、权限、资源和模型连接。",
    "H-T": "检查 Tool Schema、适配器、参数传输和结果注入。",
    "H-C": "检查 Prompt 拼接、历史裁剪和上下文注入。",
    "H-L": "检查心跳、状态机、终止条件和重试策略。",
    "H-O": "检查轨迹完整性、事件关联和错误码。",
    "H-V": "检查 Verifier、测试执行和评分逻辑。",
    "H-G": "检查权限、安全策略、预算和资源限制。",
    "L1": "检查任务说明、系统 Prompt 和规划 scaffold。",
    "L2": "比较模型能力、推理配置和任务分解策略。",
    "L3": "改进工具描述、参数 Schema 和 tool-use 行为。",
    "L4": "增加反馈处理、完成前验证和终止条件。",
}


def _average(values: Iterable[Optional[float]]) -> Optional[float]:
    items = [float(value) for value in values if value is not None]
    return sum(items) / len(items) if items else None


def _counts(
    connection: sqlite3.Connection, query: str, params: tuple
) -> Dict[str, int]:
    return {
        str(row["name"] or "UNKNOWN"): int(row["count"])
        for row in connection.execute(query, params).fetchall()
    }


def _grouped(
    connection: sqlite3.Connection, experiment_id: str, field: str
) -> Dict[str, Dict[str, int]]:
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
        """
        % (field, field),
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
        """
        SELECT verdict name, COUNT(*) count FROM trajectories
        WHERE experiment_id=? GROUP BY verdict
        """,
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
        WHERE t.experiment_id=? AND d.responsibility IS NOT NULL
        GROUP BY d.responsibility
        """,
        params,
    )
    categories = _counts(
        connection,
        """
        SELECT d.category_code name, COUNT(*) count FROM diagnoses d
        JOIN trajectories t ON t.id=d.trajectory_id
        WHERE t.experiment_id=? AND d.status='ATTRIBUTED'
          AND d.category_code IS NOT NULL
        GROUP BY d.category_code
        """,
        params,
    )
    confidence = _counts(
        connection,
        """
        SELECT d.confidence name, COUNT(*) count FROM diagnoses d
        JOIN trajectories t ON t.id=d.trajectory_id
        WHERE t.experiment_id=? AND d.confidence IS NOT NULL
        GROUP BY d.confidence
        """,
        params,
    )
    decision_sources = _counts(
        connection,
        """
        SELECT d.decision_source name, COUNT(*) count FROM diagnoses d
        JOIN trajectories t ON t.id=d.trajectory_id
        WHERE t.experiment_id=? AND d.decision_source IS NOT NULL
        GROUP BY d.decision_source
        """,
        params,
    )
    components = _counts(
        connection,
        """
        SELECT d.component name, COUNT(*) count FROM diagnoses d
        JOIN trajectories t ON t.id=d.trajectory_id
        WHERE t.experiment_id=? AND d.status='ATTRIBUTED'
          AND d.component IS NOT NULL
        GROUP BY d.component
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
    config_hashes = _counts(
        connection,
        """
        SELECT d.diagnosis_config_hash name, COUNT(*) count FROM diagnoses d
        JOIN trajectories t ON t.id=d.trajectory_id
        WHERE t.experiment_id=? AND d.diagnosis_config_hash IS NOT NULL
        GROUP BY d.diagnosis_config_hash
        """,
        params,
    )
    trajectory_modes = _counts(
        connection,
        """
        SELECT d.trajectory_mode name, COUNT(*) count FROM diagnoses d
        JOIN trajectories t ON t.id=d.trajectory_id
        WHERE t.experiment_id=? AND d.trajectory_mode IS NOT NULL
        GROUP BY d.trajectory_mode
        """,
        params,
    )
    comparable = len(config_hashes) <= 1
    return {
        "experiment": experiment_id,
        "total_tasks": len(trajectories),
        "successful_tasks": verdicts.get("PASS", 0),
        "failed_tasks": len(trajectories) - verdicts.get("PASS", 0),
        "verdicts": verdicts,
        "diagnosis_statuses": diagnosis_statuses,
        "responsibilities": responsibilities,
        "harness_layers": {
            key: value for key, value in categories.items() if key.startswith("H-")
        },
        "llm_categories": {
            key: value for key, value in categories.items() if key.startswith("L")
        },
        "confidence": confidence,
        "decision_sources": decision_sources,
        "components": components,
        "diagnosis_config_hashes": config_hashes,
        "diagnoses_comparable": comparable,
        "trajectory_modes": trajectory_modes,
        "recommended_actions": {
            code: ACTION_MAPPING[code] for code in categories if code in ACTION_MAPPING
        },
        "by_model": _grouped(connection, experiment_id, "model_name"),
        "by_agent_version": _grouped(connection, experiment_id, "agent_version"),
        "average_input_tokens": _average(row["input_tokens"] for row in trajectories),
        "average_cache_tokens": _average(row["cache_tokens"] for row in trajectories),
        "average_output_tokens": _average(row["output_tokens"] for row in trajectories),
        "average_cost": _average(row["cost"] for row in trajectories),
        "average_agent_seconds": _average(
            row["agent_execution_seconds"] for row in trajectories
        ),
        "average_verifier_seconds": _average(
            row["verifier_seconds"] for row in trajectories
        ),
        "diagnosis_input_tokens": sum(
            int(row["judge_input_tokens"] or 0) for row in diagnoses
        ),
        "diagnosis_output_tokens": sum(
            int(row["judge_output_tokens"] or 0) for row in diagnoses
        ),
        "diagnosis_latency_seconds": sum(
            float(row["judge_latency_seconds"] or 0) for row in diagnoses
        ),
    }
