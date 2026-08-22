import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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
    baseline_log_path TEXT,
    final_log_path TEXT,
    cost REAL,
    api_calls INTEGER,
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
    PRIMARY KEY (trajectory_id, step_index)
);

CREATE TABLE IF NOT EXISTS attributions (
    trajectory_id TEXT PRIMARY KEY REFERENCES trajectories(id) ON DELETE CASCADE,
    attributable INTEGER NOT NULL,
    first_error_step INTEGER,
    stage TEXT,
    mechanism TEXT,
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
    evidence_step_ids TEXT NOT NULL,
    evidence_pass INTEGER,
    notes TEXT,
    oracle_used INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
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
        task_dir = raw_path.parent
        task_id = str(data.get("task_id") or task_dir.name or raw_path.stem)
        verdict_path = task_dir / "verdict.json"
        verdict_data = read_json(verdict_path) if verdict_path.exists() else {}
        verdict = str(
            verdict_data.get("status") or data.get("verdict") or "UNKNOWN"
        ).upper()
        digest = sha256_file(raw_path)
        existing = connection.execute(
            "SELECT id FROM trajectories WHERE experiment_id=? AND task_id=?",
            (experiment_id, task_id),
        ).fetchone()
        trajectory_id = existing["id"] if existing else digest[:16]
        info = data.get("info") or {}
        stats = info.get("model_stats") or {}

        connection.execute(
            """
            INSERT INTO trajectories
            (id, experiment_id, task_id, verdict, raw_path, raw_sha256,
             final_patch_path, baseline_log_path, final_log_path, cost, api_calls)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(experiment_id, task_id) DO UPDATE SET
                verdict=excluded.verdict, raw_path=excluded.raw_path,
                raw_sha256=excluded.raw_sha256,
                final_patch_path=excluded.final_patch_path,
                baseline_log_path=excluded.baseline_log_path,
                final_log_path=excluded.final_log_path, cost=excluded.cost,
                api_calls=excluded.api_calls
            """,
            (
                trajectory_id,
                experiment_id,
                task_id,
                verdict,
                str(raw_path.resolve()),
                digest,
                str((task_dir / "final.patch").resolve())
                if (task_dir / "final.patch").exists()
                else None,
                str((task_dir / "baseline_test.log").resolve())
                if (task_dir / "baseline_test.log").exists()
                else None,
                str((task_dir / "final_test.log").resolve())
                if (task_dir / "final_test.log").exists()
                else None,
                stats.get("instance_cost"),
                stats.get("api_calls"),
            ),
        )
        connection.execute(
            "DELETE FROM steps WHERE trajectory_id = ?", (trajectory_id,)
        )
        connection.executemany(
            """
            INSERT INTO steps
            (trajectory_id, step_index, role, action_type,
             content_preview, command, test_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    trajectory_id,
                    step["step_index"],
                    step["role"],
                    step["action_type"],
                    step["content"][:4000],
                    step["command"],
                    step["test_status"],
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
        "SELECT * FROM trajectories WHERE id = ?", (trajectory_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Unknown trajectory: %s" % trajectory_id)
    return row


def get_steps(connection: sqlite3.Connection, trajectory_id: str) -> List[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM steps WHERE trajectory_id = ? ORDER BY step_index",
        (trajectory_id,),
    ).fetchall()


def failed_trajectories(
    connection: sqlite3.Connection, experiment_id: str
) -> List[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM trajectories
        WHERE experiment_id = ? AND verdict IN ('FAIL', 'TIMEOUT')
        ORDER BY task_id
        """,
        (experiment_id,),
    ).fetchall()


def save_attribution(
    connection: sqlite3.Connection, trajectory_id: str, result: Dict[str, Any]
) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO attributions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trajectory_id,
            int(bool(result["attributable"])),
            result.get("first_error_step"),
            result.get("stage"),
            result.get("mechanism"),
            result.get("summary"),
            json.dumps(result.get("evidence_step_ids", [])),
            result.get("confidence"),
            json.dumps(result, ensure_ascii=False),
            "v2",
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
) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO annotations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trajectory_id,
            split,
            first_error_step,
            stage,
            mechanism,
            json.dumps(list(evidence_step_ids)),
            None if evidence_pass is None else int(evidence_pass),
            notes,
            int(oracle_used),
            utcnow(),
        ),
    )
    connection.commit()
