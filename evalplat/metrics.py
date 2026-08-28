import sqlite3
from typing import Any, Dict, Iterable, List, Optional

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


def _trial_groups(
    connection: sqlite3.Connection, experiment_id: str
) -> Dict[str, List[sqlite3.Row]]:
    rows = connection.execute(
        """
        SELECT o.task_key, o.status, o.reward, t.id, t.trial_name,
               t.cost, t.agent_execution_seconds
        FROM outcomes o JOIN trajectories t ON t.id=o.trajectory_id
        WHERE o.experiment_id=?
        ORDER BY o.task_key, COALESCE(t.finished_at, ''), t.trial_name, t.id
        """,
        (experiment_id,),
    ).fetchall()
    groups: Dict[str, List[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(str(row["task_key"]), []).append(row)
    return groups


def _rate(matches: Iterable[bool]) -> Optional[float]:
    values = list(matches)
    return sum(values) / len(values) if values else None


def _outcome_metrics(
    connection: sqlite3.Connection, experiment_id: str
) -> Dict[str, Any]:
    groups = _trial_groups(connection, experiment_id)
    trials = [row for rows in groups.values() for row in rows]
    checks = connection.execute(
        """
        SELECT c.status, c.weight FROM checks c
        JOIN trajectories t ON t.id=c.trajectory_id
        WHERE t.experiment_id=?
        """,
        (experiment_id,),
    ).fetchall()
    known_checks = [row for row in checks if row["status"] != "UNKNOWN"]
    total_weight = sum(float(row["weight"]) for row in known_checks)
    passed_weight = sum(
        float(row["weight"]) for row in known_checks if row["status"] == "PASS"
    )
    successful_tasks = sum(
        any(row["status"] == "PASS" for row in rows) for rows in groups.values()
    )
    successful_trials = sum(row["status"] == "PASS" for row in trials)
    return {
        "logical_tasks": len(groups),
        "total_trials": len(trials),
        "successful_tasks": successful_tasks,
        "failed_tasks": len(groups) - successful_tasks,
        "successful_trials": successful_trials,
        "failed_trials": len(trials) - successful_trials,
        "trial_pass_rate": _rate(row["status"] == "PASS" for row in trials),
        "task_any_pass_rate": _rate(
            any(row["status"] == "PASS" for row in rows) for rows in groups.values()
        ),
        "task_all_pass_rate": _rate(
            all(row["status"] == "PASS" for row in rows) for rows in groups.values()
        ),
        "check_statuses": {
            status: sum(row["status"] == status for row in checks)
            for status in ("PASS", "FAIL", "UNKNOWN")
            if any(row["status"] == status for row in checks)
        },
        "weighted_check_pass_rate": (
            passed_weight / total_weight if total_weight else None
        ),
    }


def _relative_change(baseline: Optional[float], candidate: Optional[float]):
    if baseline is None or candidate is None or baseline == 0:
        return None
    return (candidate - baseline) / baseline


def compare_experiments(
    connection: sqlite3.Connection,
    baseline_id: str,
    candidate_id: str,
    k: int = 1,
    max_cost_increase: float = 0.2,
    max_regressions: int = 0,
    max_pass_at_k_drop: float = 0.0,
) -> Dict[str, Any]:
    if k <= 0:
        raise ValueError("k must be positive")
    if max_cost_increase < 0:
        raise ValueError("max_cost_increase must be non-negative")
    if max_regressions < 0:
        raise ValueError("max_regressions must be non-negative")
    if max_pass_at_k_drop < 0:
        raise ValueError("max_pass_at_k_drop must be non-negative")
    baseline = _trial_groups(connection, baseline_id)
    candidate = _trial_groups(connection, candidate_id)
    if not baseline:
        raise ValueError("Unknown or empty experiment: %s" % baseline_id)
    if not candidate:
        raise ValueError("Unknown or empty experiment: %s" % candidate_id)
    shared = sorted(set(baseline) & set(candidate))
    eligible = [
        key for key in shared if len(baseline[key]) >= k and len(candidate[key]) >= k
    ]
    if not eligible:
        raise ValueError("No shared tasks have at least %s trial(s) per experiment" % k)

    def selected(groups, key):
        return groups[key][:k]

    def any_pass(rows):
        return any(row["status"] == "PASS" for row in rows)

    def all_pass(rows):
        return all(row["status"] == "PASS" for row in rows)

    baseline_any = [any_pass(selected(baseline, key)) for key in eligible]
    candidate_any = [any_pass(selected(candidate, key)) for key in eligible]
    baseline_all = [all_pass(selected(baseline, key)) for key in eligible]
    candidate_all = [all_pass(selected(candidate, key)) for key in eligible]
    details = []
    for index, key in enumerate(eligible):
        before, after = baseline_any[index], candidate_any[index]
        change = (
            "IMPROVED"
            if after and not before
            else ("REGRESSED" if before and not after else "UNCHANGED")
        )
        details.append(
            {
                "task_key": key,
                "change": change,
                "baseline_passes": sum(
                    row["status"] == "PASS" for row in selected(baseline, key)
                ),
                "candidate_passes": sum(
                    row["status"] == "PASS" for row in selected(candidate, key)
                ),
                "baseline_statuses": [
                    row["status"] for row in selected(baseline, key)
                ],
                "candidate_statuses": [
                    row["status"] for row in selected(candidate, key)
                ],
            }
        )
    baseline_rows = [row for key in eligible for row in selected(baseline, key)]
    candidate_rows = [row for key in eligible for row in selected(candidate, key)]
    baseline_cost = _average(row["cost"] for row in baseline_rows)
    candidate_cost = _average(row["cost"] for row in candidate_rows)
    baseline_latency = _average(row["agent_execution_seconds"] for row in baseline_rows)
    candidate_latency = _average(
        row["agent_execution_seconds"] for row in candidate_rows
    )
    cost_change = _relative_change(baseline_cost, candidate_cost)
    regressions = sum(item["change"] == "REGRESSED" for item in details)
    pass_at_k_baseline = _rate(baseline_any)
    pass_at_k_candidate = _rate(candidate_any)
    reasons = []
    if regressions > max_regressions:
        reasons.append(
            "%s shared task(s) regressed; limit is %s"
            % (regressions, max_regressions)
        )
    if pass_at_k_baseline - pass_at_k_candidate > max_pass_at_k_drop:
        reasons.append("candidate pass@k drop exceeded threshold")
    if cost_change is not None and cost_change > max_cost_increase:
        reasons.append("candidate average cost increased beyond threshold")
    return {
        "baseline": baseline_id,
        "candidate": candidate_id,
        "k": k,
        "thresholds": {
            "max_regressions": max_regressions,
            "max_pass_at_k_drop": max_pass_at_k_drop,
            "max_cost_increase": max_cost_increase,
        },
        "shared_tasks": len(shared),
        "eligible_tasks": len(eligible),
        "baseline_metrics": {
            "pass_at_k": pass_at_k_baseline,
            "pass_power_k": _rate(baseline_all),
            "average_cost": baseline_cost,
            "average_agent_seconds": baseline_latency,
        },
        "candidate_metrics": {
            "pass_at_k": pass_at_k_candidate,
            "pass_power_k": _rate(candidate_all),
            "average_cost": candidate_cost,
            "average_agent_seconds": candidate_latency,
        },
        "deltas": {
            "pass_at_k": pass_at_k_candidate - pass_at_k_baseline,
            "pass_power_k": _rate(candidate_all) - _rate(baseline_all),
            "cost_relative": cost_change,
            "agent_seconds_relative": _relative_change(
                baseline_latency, candidate_latency
            ),
        },
        "changes": {
            "improved": sum(item["change"] == "IMPROVED" for item in details),
            "regressed": regressions,
            "unchanged": sum(item["change"] == "UNCHANGED" for item in details),
        },
        "ship_gate": {"status": "FAIL" if reasons else "PASS", "reasons": reasons},
        "tasks": details,
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
    outcome_metrics = _outcome_metrics(connection, experiment_id)
    return {
        "experiment": experiment_id,
        "total_tasks": outcome_metrics["logical_tasks"],
        "total_trials": outcome_metrics["total_trials"],
        "successful_tasks": outcome_metrics["successful_tasks"],
        "failed_tasks": outcome_metrics["failed_tasks"],
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
        **outcome_metrics,
        "recommended_actions": {
            code: ACTION_MAPPING[code] for code in categories if code in ACTION_MAPPING
        },
        "by_model": _grouped(connection, experiment_id, "model_name"),
        "by_agent": _grouped(connection, experiment_id, "agent_name"),
        "by_agent_version": _grouped(connection, experiment_id, "agent_version"),
        "by_dataset": _grouped(connection, experiment_id, "source_dataset"),
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
