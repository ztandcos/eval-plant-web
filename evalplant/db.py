import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core import normalize_trajectory, read_json, sha256_file

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    agent_model TEXT,
    judge_model TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trajectories (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    task_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    final_patch_path TEXT,
    final_log_path TEXT,
    cost REAL,
    api_calls INTEGER,
    base_task_id TEXT,
    trial_name TEXT,
    health_status TEXT,
    reward REAL,
    raw_event_path TEXT,
    raw_event_sha256 TEXT,
    agent_version TEXT,
    model_name TEXT,
    started_at TEXT,
    finished_at TEXT,
    input_tokens INTEGER,
    cache_tokens INTEGER,
    output_tokens INTEGER,
    environment_setup_seconds REAL,
    agent_setup_seconds REAL,
    agent_execution_seconds REAL,
    verifier_seconds REAL,
    UNIQUE(experiment_id, task_id)
);

CREATE TABLE IF NOT EXISTS steps (
    trajectory_id TEXT NOT NULL REFERENCES trajectories(id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL,
    role TEXT NOT NULL,
    action_type TEXT NOT NULL,
    content_preview TEXT NOT NULL,
    command TEXT,
    test_status TEXT,
    tool_name TEXT,
    tool_arguments TEXT,
    PRIMARY KEY (trajectory_id, step_index)
);

CREATE TABLE IF NOT EXISTS diagnoses (
    trajectory_id TEXT PRIMARY KEY REFERENCES trajectories(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    responsibility TEXT,
    category_code TEXT,
    category_name TEXT,
    root_cause_step INTEGER,
    component TEXT,
    summary TEXT NOT NULL,
    confidence TEXT,
    decision_source TEXT,
    matched_rule TEXT,
    judge_model TEXT,
    prompt_version TEXT NOT NULL,
    judge_input_tokens INTEGER NOT NULL DEFAULT 0,
    judge_output_tokens INTEGER NOT NULL DEFAULT 0,
    judge_latency_seconds REAL NOT NULL DEFAULT 0,
    judge_thinking TEXT,
    judge_max_input_tokens INTEGER,
    judge_max_output_tokens INTEGER,
    diagnosis_error TEXT,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

TRAJECTORY_MIGRATIONS = {
    "base_task_id": "TEXT",
    "trial_name": "TEXT",
    "health_status": "TEXT",
    "reward": "REAL",
    "raw_event_path": "TEXT",
    "raw_event_sha256": "TEXT",
    "agent_version": "TEXT",
    "model_name": "TEXT",
    "started_at": "TEXT",
    "finished_at": "TEXT",
    "input_tokens": "INTEGER",
    "cache_tokens": "INTEGER",
    "output_tokens": "INTEGER",
    "environment_setup_seconds": "REAL",
    "agent_setup_seconds": "REAL",
    "agent_execution_seconds": "REAL",
    "verifier_seconds": "REAL",
}

DIAGNOSIS_MIGRATIONS = {
    "judge_thinking": "TEXT",
    "judge_max_input_tokens": "INTEGER",
    "judge_max_output_tokens": "INTEGER",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    existing = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(trajectories)").fetchall()
    }
    for name, definition in TRAJECTORY_MIGRATIONS.items():
        if name not in existing:
            connection.execute(
                "ALTER TABLE trajectories ADD COLUMN %s %s" % (name, definition)
            )
    diagnosis_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(diagnoses)")
    }
    for name, definition in DIAGNOSIS_MIGRATIONS.items():
        if name not in diagnosis_columns:
            connection.execute(
                "ALTER TABLE diagnoses ADD COLUMN %s %s" % (name, definition)
            )
    connection.execute(
        "UPDATE trajectories SET base_task_id=task_id WHERE base_task_id IS NULL"
    )
    connection.execute(
        """
        UPDATE trajectories
        SET health_status=CASE
            WHEN verdict='INFRA_ERROR' THEN 'INFRA_ERROR' ELSE 'VALID' END
        WHERE health_status IS NULL
        """
    )
    connection.execute(
        """
        UPDATE trajectories SET reward=CASE
            WHEN verdict='PASS' THEN 1.0 WHEN verdict='FAIL' THEN 0.0 END
        WHERE reward IS NULL AND verdict IN ('PASS', 'FAIL')
        """
    )
    connection.commit()
    return connection


def ensure_experiment(
    connection: sqlite3.Connection,
    experiment_id: str,
    agent_model: Optional[str] = None,
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO experiments VALUES (?, ?, ?, ?)",
        (experiment_id, agent_model, None, utcnow()),
    )
    if agent_model:
        connection.execute(
            "UPDATE experiments SET agent_model=? WHERE id=?",
            (agent_model, experiment_id),
        )
    connection.commit()


def _trajectory_paths(path: Path) -> List[Path]:
    if path.is_file():
        return [path]
    paths = set(path.rglob("*.traj.json"))
    paths.update(path.rglob("trajectory.json"))
    return sorted(paths)


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _duration_seconds(value: Any) -> Optional[float]:
    if not isinstance(value, dict) or not value.get("started_at") or not value.get("finished_at"):
        return None
    try:
        start = datetime.fromisoformat(str(value["started_at"]).replace("Z", "+00:00"))
        finish = datetime.fromisoformat(str(value["finished_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (finish - start).total_seconds())


def _harbor_metadata(raw_path: Path, data: Dict[str, Any]) -> Dict[str, Any]:
    trial_dir = raw_path.parent.parent
    result_path = trial_dir / "result.json"
    result = read_json(result_path) if result_path.exists() else {}
    exception = result.get("exception_info") or {}
    verifier = result.get("verifier_result") or {}
    rewards = verifier.get("rewards") or {}
    values = [_number(value) for value in rewards.values()]
    values = [value for value in values if value is not None]
    reward = sum(values) / len(values) if values else None
    exception_type = str(exception.get("exception_type") or "")
    if exception:
        verdict = "TIMEOUT" if "timeout" in exception_type.lower() else "INFRA_ERROR"
        health = "VALID" if verdict == "TIMEOUT" else "INFRA_ERROR"
    elif verifier and reward is not None:
        verdict, health = ("PASS" if reward == 1 else "FAIL"), "VALID"
    elif result.get("agent_result"):
        verdict, health = "UNKNOWN", "VALID"
    else:
        verdict, health = "UNKNOWN", "INCOMPLETE"

    agent = data.get("agent") or {}
    result_agent = result.get("agent_info") or {}
    model_info = result_agent.get("model_info") or {}
    task_id = str(result.get("task_name") or trial_dir.name)
    trial_name = str(result.get("trial_name") or trial_dir.name)
    raw_events = sorted((trial_dir / "agent").rglob("session.jsonl"))
    final_logs = [
        trial_dir / "verifier" / "test-stdout.txt",
        trial_dir / "agent" / "dsh-minimal.txt",
    ]
    agent_result = result.get("agent_result") or {}
    return {
        "base_task_id": task_id,
        "storage_task_id": "%s::%s" % (task_id, trial_name),
        "trajectory_id": str(result.get("id") or sha256_file(raw_path)[:16]),
        "trial_name": trial_name,
        "verdict": verdict,
        "health_status": health,
        "reward": reward,
        "raw_event_path": raw_events[0] if raw_events else None,
        "agent_version": agent.get("version") or result_agent.get("version"),
        "model_name": agent.get("model_name") or model_info.get("name"),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "final_log_path": next((path for path in final_logs if path.exists()), None),
        "cost": agent_result.get("cost_usd"),
        "api_calls": agent_result.get("api_calls"),
        "input_tokens": agent_result.get("n_input_tokens"),
        "cache_tokens": agent_result.get("n_cache_tokens"),
        "output_tokens": agent_result.get("n_output_tokens"),
        "environment_setup_seconds": _duration_seconds(result.get("environment_setup")),
        "agent_setup_seconds": _duration_seconds(result.get("agent_setup")),
        "agent_execution_seconds": _duration_seconds(result.get("agent_execution")),
        "verifier_seconds": _duration_seconds(result.get("verifier")),
    }


def _legacy_metadata(raw_path: Path, data: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str(data.get("task_id") or raw_path.parent.name or raw_path.stem)
    verdict = str(data.get("verdict") or "UNKNOWN").upper()
    stats = (data.get("info") or {}).get("model_stats") or {}
    return {
        "base_task_id": task_id,
        "storage_task_id": task_id,
        "trajectory_id": sha256_file(raw_path)[:16],
        "trial_name": task_id,
        "verdict": verdict,
        "health_status": "INFRA_ERROR" if verdict == "INFRA_ERROR" else "VALID",
        "reward": 1.0 if verdict == "PASS" else (0.0 if verdict == "FAIL" else None),
        "raw_event_path": None,
        "agent_version": None,
        "model_name": None,
        "started_at": None,
        "finished_at": None,
        "final_log_path": raw_path.parent / "final_test.log",
        "cost": stats.get("instance_cost"),
        "api_calls": stats.get("api_calls"),
        "input_tokens": stats.get("input_tokens"),
        "cache_tokens": stats.get("cache_tokens"),
        "output_tokens": stats.get("output_tokens"),
        "environment_setup_seconds": None,
        "agent_setup_seconds": None,
        "agent_execution_seconds": None,
        "verifier_seconds": None,
    }


def import_run(
    connection: sqlite3.Connection,
    path: Path,
    experiment_id: str,
    agent_model: Optional[str] = None,
) -> List[str]:
    ensure_experiment(connection, experiment_id, agent_model)
    trajectory_ids = []
    for raw_path in _trajectory_paths(path):
        data = read_json(raw_path)
        steps = normalize_trajectory(data)
        is_atif = str(data.get("schema_version") or "").startswith("ATIF-")
        metadata = _harbor_metadata(raw_path, data) if is_atif else _legacy_metadata(raw_path, data)
        existing = connection.execute(
            "SELECT id FROM trajectories WHERE experiment_id=? AND task_id=?",
            (experiment_id, metadata["storage_task_id"]),
        ).fetchone()
        trajectory_id = existing["id"] if existing else metadata["trajectory_id"]
        raw_digest = sha256_file(raw_path)
        occupied = connection.execute(
            "SELECT experiment_id, task_id FROM trajectories WHERE id=?",
            (trajectory_id,),
        ).fetchone()
        if not existing and occupied:
            seed = "%s\0%s\0%s" % (
                experiment_id,
                metadata["storage_task_id"],
                raw_digest,
            )
            trajectory_id = hashlib.sha256(seed.encode()).hexdigest()[:32]
        task_dir = raw_path.parent.parent if is_atif else raw_path.parent
        final_patch = task_dir / "final.patch"
        final_log = metadata["final_log_path"]
        raw_event = metadata["raw_event_path"]
        connection.execute(
            """
            INSERT INTO trajectories (
                id, experiment_id, task_id, verdict, raw_path, raw_sha256,
                final_patch_path, final_log_path, cost, api_calls, base_task_id,
                trial_name, health_status, reward, raw_event_path, raw_event_sha256,
                agent_version, model_name, started_at, finished_at, input_tokens,
                cache_tokens, output_tokens, environment_setup_seconds,
                agent_setup_seconds, agent_execution_seconds, verifier_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?)
            ON CONFLICT(experiment_id, task_id) DO UPDATE SET
                verdict=excluded.verdict, raw_path=excluded.raw_path,
                raw_sha256=excluded.raw_sha256, final_patch_path=excluded.final_patch_path,
                final_log_path=excluded.final_log_path, cost=excluded.cost,
                api_calls=excluded.api_calls, base_task_id=excluded.base_task_id,
                trial_name=excluded.trial_name, health_status=excluded.health_status,
                reward=excluded.reward, raw_event_path=excluded.raw_event_path,
                raw_event_sha256=excluded.raw_event_sha256,
                agent_version=excluded.agent_version, model_name=excluded.model_name,
                started_at=excluded.started_at, finished_at=excluded.finished_at,
                input_tokens=excluded.input_tokens, cache_tokens=excluded.cache_tokens,
                output_tokens=excluded.output_tokens,
                environment_setup_seconds=excluded.environment_setup_seconds,
                agent_setup_seconds=excluded.agent_setup_seconds,
                agent_execution_seconds=excluded.agent_execution_seconds,
                verifier_seconds=excluded.verifier_seconds
            """,
            (
                trajectory_id,
                experiment_id,
                metadata["storage_task_id"],
                metadata["verdict"],
                str(raw_path.resolve()),
                raw_digest,
                str(final_patch.resolve()) if final_patch.exists() else None,
                str(final_log.resolve()) if final_log and final_log.exists() else None,
                metadata["cost"], metadata["api_calls"], metadata["base_task_id"],
                metadata["trial_name"], metadata["health_status"], metadata["reward"],
                str(raw_event.resolve()) if raw_event else None,
                sha256_file(raw_event) if raw_event else None,
                metadata["agent_version"], metadata["model_name"], metadata["started_at"],
                metadata["finished_at"], metadata["input_tokens"], metadata["cache_tokens"],
                metadata["output_tokens"], metadata["environment_setup_seconds"],
                metadata["agent_setup_seconds"], metadata["agent_execution_seconds"],
                metadata["verifier_seconds"],
            ),
        )
        connection.execute("DELETE FROM steps WHERE trajectory_id=?", (trajectory_id,))
        connection.execute("DELETE FROM diagnoses WHERE trajectory_id=?", (trajectory_id,))
        connection.executemany(
            """
            INSERT INTO steps (
                trajectory_id, step_index, role, action_type, content_preview,
                command, test_status, tool_name, tool_arguments
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    trajectory_id, step["step_index"], step["role"], step["action_type"],
                    step["content"][:12000], step["command"], step["test_status"],
                    step.get("tool_name"),
                    json.dumps(step.get("tool_arguments"), ensure_ascii=False),
                )
                for step in steps
            ],
        )
        connection.commit()
        trajectory_ids.append(trajectory_id)
    if not trajectory_ids:
        raise ValueError("No trajectory JSON files found under %s" % path)
    return trajectory_ids


def get_trajectory(connection: sqlite3.Connection, trajectory_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM trajectories WHERE id=?", (trajectory_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown trajectory: %s" % trajectory_id)
    return row


def get_steps(connection: sqlite3.Connection, trajectory_id: str) -> List[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM steps WHERE trajectory_id=? ORDER BY step_index", (trajectory_id,)
    ).fetchall()


def get_diagnosis(connection: sqlite3.Connection, trajectory_id: str) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM diagnoses WHERE trajectory_id=?", (trajectory_id,)
    ).fetchone()


def diagnosable_trajectories(connection: sqlite3.Connection, experiment_id: str) -> List[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM trajectories
        WHERE experiment_id=? AND verdict IN ('FAIL', 'TIMEOUT', 'INFRA_ERROR', 'UNKNOWN', 'INCOMPLETE')
        ORDER BY base_task_id, trial_name
        """,
        (experiment_id,),
    ).fetchall()


def save_diagnosis(connection: sqlite3.Connection, trajectory_id: str, report: Dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO diagnoses (
            trajectory_id, status, responsibility, category_code, category_name,
            root_cause_step, component, summary, confidence, decision_source,
            matched_rule, judge_model, prompt_version, judge_input_tokens,
            judge_output_tokens, judge_latency_seconds, judge_thinking,
            judge_max_input_tokens, judge_max_output_tokens, diagnosis_error,
            report_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trajectory_id, report["status"], report.get("responsibility"),
            report.get("category_code"), report.get("category_name"),
            report.get("root_cause_step"), report.get("component"), report.get("summary") or "",
            report.get("confidence"), report.get("decision_source"), report.get("matched_rule"),
            report.get("judge_model"), report.get("prompt_version") or "unknown",
            int(report.get("judge_input_tokens") or 0), int(report.get("judge_output_tokens") or 0),
            float(report.get("judge_latency_seconds") or 0), report.get("judge_thinking"),
            report.get("max_input_tokens"), report.get("max_output_tokens"),
            report.get("diagnosis_error"),
            json.dumps(report, ensure_ascii=False), utcnow(),
        ),
    )
    connection.commit()
