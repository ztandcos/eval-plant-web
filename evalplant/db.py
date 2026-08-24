import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .core import normalize_trajectory, read_json, sha256_file
from .core import validate_taxonomy

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
    baseline_log_path TEXT,
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

CREATE TABLE IF NOT EXISTS attributions (
    trajectory_id TEXT PRIMARY KEY REFERENCES trajectories(id) ON DELETE CASCADE,
    attributable INTEGER NOT NULL,
    first_error_step INTEGER,
    stage TEXT,
    mechanism TEXT,
    subcategory TEXT,
    summary TEXT,
    evidence_step_ids TEXT NOT NULL,
    confidence REAL,
    raw_json TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS annotations (
    trajectory_id TEXT PRIMARY KEY REFERENCES trajectories(id) ON DELETE CASCADE,
    split TEXT NOT NULL,
    first_error_step INTEGER NOT NULL,
    stage TEXT NOT NULL,
    mechanism TEXT NOT NULL,
    subcategory TEXT,
    evidence_step_ids TEXT NOT NULL,
    evidence_pass INTEGER,
    notes TEXT,
    oracle_used INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attribution_jobs (
    trajectory_id TEXT PRIMARY KEY REFERENCES trajectories(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'RUNNING', 'DONE', 'FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    updated_at TEXT NOT NULL
);
"""

MIGRATIONS = {
    "trajectories": {
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
    },
    "steps": {"tool_name": "TEXT", "tool_arguments": "TEXT"},
    "attributions": {"subcategory": "TEXT"},
    "annotations": {"subcategory": "TEXT"},
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        existing = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(%s)" % table).fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(
                    "ALTER TABLE %s ADD COLUMN %s %s" % (table, name, definition)
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
    judge_model: Optional[str] = None,
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO experiments VALUES (?, ?, ?, ?)",
        (experiment_id, agent_model, judge_model, utcnow()),
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
    if not isinstance(value, dict):
        return None
    started = value.get("started_at")
    finished = value.get("finished_at")
    if not started or not finished:
        return None
    try:
        start = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        finish = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
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
    reward_values = [_number(value) for value in rewards.values()]
    reward_values = [value for value in reward_values if value is not None]
    reward = sum(reward_values) / len(reward_values) if reward_values else None
    exception_type = str(exception.get("exception_type") or "")
    if exception:
        verdict = "TIMEOUT" if "timeout" in exception_type.lower() else "INFRA_ERROR"
        health = "VALID" if verdict == "TIMEOUT" else "INFRA_ERROR"
    elif verifier and reward is not None:
        verdict = "PASS" if reward == 1 else "FAIL"
        health = "VALID"
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
        "api_calls": None,
        "input_tokens": agent_result.get("n_input_tokens"),
        "cache_tokens": agent_result.get("n_cache_tokens"),
        "output_tokens": agent_result.get("n_output_tokens"),
        "environment_setup_seconds": _duration_seconds(result.get("environment_setup")),
        "agent_setup_seconds": _duration_seconds(result.get("agent_setup")),
        "agent_execution_seconds": _duration_seconds(result.get("agent_execution")),
        "verifier_seconds": _duration_seconds(result.get("verifier")),
    }


def _legacy_metadata(raw_path: Path, data: Dict[str, Any]) -> Dict[str, Any]:
    task_dir = raw_path.parent
    task_id = str(data.get("task_id") or task_dir.name or raw_path.stem)
    verdict_path = task_dir / "verdict.json"
    verdict_data = read_json(verdict_path) if verdict_path.exists() else {}
    verdict = str(
        verdict_data.get("status") or data.get("verdict") or "UNKNOWN"
    ).upper()
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
        "final_log_path": task_dir / "final_test.log",
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
    ensure_experiment(connection, experiment_id, agent_model=agent_model)
    trajectory_ids = []
    for raw_path in _trajectory_paths(path):
        data = read_json(raw_path)
        steps = normalize_trajectory(data)
        is_atif = str(data.get("schema_version") or "").startswith("ATIF-")
        metadata = (
            _harbor_metadata(raw_path, data)
            if is_atif
            else _legacy_metadata(raw_path, data)
        )
        digest = sha256_file(raw_path)
        existing = connection.execute(
            "SELECT id FROM trajectories WHERE experiment_id=? AND task_id=?",
            (experiment_id, metadata["storage_task_id"]),
        ).fetchone()
        trajectory_id = existing["id"] if existing else metadata["trajectory_id"]
        raw_event = metadata["raw_event_path"]
        task_dir = raw_path.parent.parent if is_atif else raw_path.parent
        final_patch = task_dir / "final.patch"
        baseline_log = task_dir / "baseline_test.log"
        final_log = metadata["final_log_path"]

        connection.execute(
            """
            INSERT INTO trajectories (
                id, experiment_id, task_id, verdict, raw_path, raw_sha256,
                final_patch_path, baseline_log_path, final_log_path, cost, api_calls,
                base_task_id, trial_name, health_status, reward, raw_event_path,
                raw_event_sha256, agent_version, model_name, started_at, finished_at,
                input_tokens, cache_tokens, output_tokens, environment_setup_seconds,
                agent_setup_seconds, agent_execution_seconds, verifier_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(experiment_id, task_id) DO UPDATE SET
                verdict=excluded.verdict, raw_path=excluded.raw_path,
                raw_sha256=excluded.raw_sha256, final_patch_path=excluded.final_patch_path,
                baseline_log_path=excluded.baseline_log_path,
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
                digest,
                str(final_patch.resolve()) if final_patch.exists() else None,
                str(baseline_log.resolve()) if baseline_log.exists() else None,
                str(final_log.resolve()) if final_log and final_log.exists() else None,
                metadata["cost"],
                metadata["api_calls"],
                metadata["base_task_id"],
                metadata["trial_name"],
                metadata["health_status"],
                metadata["reward"],
                str(raw_event.resolve()) if raw_event else None,
                sha256_file(raw_event) if raw_event else None,
                metadata["agent_version"],
                metadata["model_name"],
                metadata["started_at"],
                metadata["finished_at"],
                metadata["input_tokens"],
                metadata["cache_tokens"],
                metadata["output_tokens"],
                metadata["environment_setup_seconds"],
                metadata["agent_setup_seconds"],
                metadata["agent_execution_seconds"],
                metadata["verifier_seconds"],
            ),
        )
        connection.execute("DELETE FROM steps WHERE trajectory_id=?", (trajectory_id,))
        connection.executemany(
            """
            INSERT INTO steps (
                trajectory_id, step_index, role, action_type, content_preview,
                command, test_status, tool_name, tool_arguments
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    trajectory_id,
                    step["step_index"],
                    step["role"],
                    step["action_type"],
                    step["content"][:12000],
                    step["command"],
                    step["test_status"],
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
    row = connection.execute(
        "SELECT * FROM trajectories WHERE id=?", (trajectory_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Unknown trajectory: %s" % trajectory_id)
    return row


def get_steps(connection: sqlite3.Connection, trajectory_id: str) -> List[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM steps WHERE trajectory_id=? ORDER BY step_index",
        (trajectory_id,),
    ).fetchall()


def failed_trajectories(
    connection: sqlite3.Connection, experiment_id: str
) -> List[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM trajectories
        WHERE experiment_id=? AND health_status='VALID' AND verdict IN ('FAIL', 'TIMEOUT')
        ORDER BY base_task_id, trial_name
        """,
        (experiment_id,),
    ).fetchall()


def enqueue_attribution(connection: sqlite3.Connection, trajectory_id: str) -> bool:
    row = get_trajectory(connection, trajectory_id)
    if row["health_status"] != "VALID" or row["verdict"] not in ("FAIL", "TIMEOUT"):
        return False
    connection.execute(
        """
        INSERT INTO attribution_jobs (trajectory_id, status, updated_at)
        VALUES (?, 'PENDING', ?)
        ON CONFLICT(trajectory_id) DO NOTHING
        """,
        (trajectory_id, utcnow()),
    )
    connection.commit()
    return True


def claim_attribution_job(connection: sqlite3.Connection) -> Optional[sqlite3.Row]:
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute(
        """
        SELECT t.* FROM attribution_jobs j
        JOIN trajectories t ON t.id=j.trajectory_id
        WHERE j.status='PENDING' ORDER BY j.updated_at LIMIT 1
        """
    ).fetchone()
    if row is not None:
        connection.execute(
            """
            UPDATE attribution_jobs
            SET status='RUNNING', attempts=attempts+1, updated_at=?
            WHERE trajectory_id=?
            """,
            (utcnow(), row["id"]),
        )
    connection.commit()
    return row


def finish_attribution_job(
    connection: sqlite3.Connection,
    trajectory_id: str,
    error: Optional[str] = None,
) -> None:
    connection.execute(
        """
        UPDATE attribution_jobs SET status=?, error=?, updated_at=?
        WHERE trajectory_id=?
        """,
        ("FAILED" if error else "DONE", error, utcnow(), trajectory_id),
    )
    connection.commit()


def save_attribution(
    connection: sqlite3.Connection, trajectory_id: str, result: Dict[str, Any]
) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO attributions (
            trajectory_id, attributable, first_error_step, stage, mechanism,
            subcategory, summary, evidence_step_ids, confidence, raw_json,
            prompt_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trajectory_id,
            int(bool(result["attributable"])),
            result.get("first_error_step"),
            result.get("stage"),
            result.get("mechanism"),
            result.get("subcategory"),
            result.get("summary"),
            json.dumps(result.get("evidence_step_ids", [])),
            result.get("confidence"),
            json.dumps(result, ensure_ascii=False),
            "v3",
            utcnow(),
        ),
    )
    connection.commit()


def save_annotation(
    connection: sqlite3.Connection,
    trajectory_id: str,
    split: str,
    first_error_step: int,
    stage: str,
    mechanism: str,
    evidence_step_ids: Iterable[int],
    evidence_pass: Optional[bool],
    notes: str,
    oracle_used: bool,
    subcategory: Optional[str] = None,
) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO annotations (
            trajectory_id, split, first_error_step, stage, mechanism, subcategory,
            evidence_step_ids, evidence_pass, notes, oracle_used, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trajectory_id,
            split,
            first_error_step,
            stage,
            mechanism,
            subcategory,
            json.dumps(list(evidence_step_ids)),
            None if evidence_pass is None else int(evidence_pass),
            notes,
            int(oracle_used),
            utcnow(),
        ),
    )
    connection.commit()


ANNOTATION_COLUMNS = (
    "trajectory_id",
    "task_id",
    "raw_path",
    "verifier_log_path",
    "judge_step",
    "judge_stage",
    "judge_mechanism",
    "judge_subcategory",
    "judge_summary",
    "human_step",
    "human_stage",
    "human_mechanism",
    "human_subcategory",
    "human_evidence_steps",
    "evidence_pass",
    "notes",
    "oracle_used",
    "split",
)


def export_annotation_template(
    connection: sqlite3.Connection, experiment_id: str, output_path: Path
) -> int:
    rows = connection.execute(
        """
        SELECT t.id trajectory_id, t.base_task_id task_id, t.raw_path,
               t.final_log_path verifier_log_path,
               a.first_error_step judge_step, a.stage judge_stage,
               a.mechanism judge_mechanism, a.subcategory judge_subcategory,
               a.summary judge_summary
        FROM trajectories t
        LEFT JOIN attributions a ON a.trajectory_id=t.id
        WHERE t.experiment_id=? AND t.health_status='VALID'
          AND t.verdict IN ('FAIL', 'TIMEOUT')
        ORDER BY t.base_task_id, t.trial_name
        """,
        (experiment_id,),
    ).fetchall()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANNOTATION_COLUMNS)
        writer.writeheader()
        for row in rows:
            item = {name: "" for name in ANNOTATION_COLUMNS}
            item.update(dict(row))
            item["split"] = "test"
            item["oracle_used"] = "no"
            writer.writerow(item)
    return len(rows)


def import_annotations(connection: sqlite3.Connection, input_path: Path) -> int:
    count = 0
    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for line_number, row in enumerate(csv.DictReader(handle), start=2):
            trajectory_id = str(row.get("trajectory_id") or "").strip()
            step_text = str(row.get("human_step") or "").strip()
            stage = str(row.get("human_stage") or "").strip()
            mechanism = str(row.get("human_mechanism") or "").strip()
            subcategory = str(row.get("human_subcategory") or "").strip()
            if not all((trajectory_id, step_text, stage, mechanism, subcategory)):
                continue
            try:
                step = int(step_text)
                evidence = [
                    int(value.strip())
                    for value in str(row.get("human_evidence_steps") or "").split(",")
                    if value.strip()
                ]
            except ValueError as error:
                raise ValueError("Invalid step on CSV line %s" % line_number) from error
            valid_steps = {
                item["step_index"] for item in get_steps(connection, trajectory_id)
            }
            if step not in valid_steps or any(
                value not in valid_steps for value in evidence
            ):
                raise ValueError("Unknown evidence step on CSV line %s" % line_number)
            validate_taxonomy(stage, mechanism, subcategory)
            evidence_text = str(row.get("evidence_pass") or "").strip().lower()
            if evidence_text not in ("", "yes", "no"):
                raise ValueError(
                    "evidence_pass must be yes or no on line %s" % line_number
                )
            save_annotation(
                connection,
                trajectory_id,
                str(row.get("split") or "test").strip(),
                step,
                stage,
                mechanism,
                evidence,
                None if not evidence_text else evidence_text == "yes",
                str(row.get("notes") or ""),
                str(row.get("oracle_used") or "no").strip().lower() == "yes",
                subcategory,
            )
            count += 1
    return count
