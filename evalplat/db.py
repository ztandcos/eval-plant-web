import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .core import (
    ADAPTER_VERSION,
    CANONICAL_SCHEMA_VERSION,
    normalize_trajectory,
    read_json,
    sanitize_value,
    sha256_file,
    validate_trajectory_schema,
)

DATABASE_SCHEMA_VERSION = 7

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
    agent_name TEXT,
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
    source_schema_version TEXT,
    canonical_schema_version TEXT,
    adapter_version TEXT,
    source_dataset TEXT,
    source_instance_id TEXT,
    UNIQUE(experiment_id, task_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    task_key TEXT NOT NULL,
    source_dataset TEXT,
    source_instance_id TEXT,
    success_threshold REAL NOT NULL DEFAULT 1.0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (experiment_id, task_key)
);

CREATE TABLE IF NOT EXISTS outcomes (
    trajectory_id TEXT PRIMARY KEY REFERENCES trajectories(id) ON DELETE CASCADE,
    experiment_id TEXT NOT NULL,
    task_key TEXT NOT NULL,
    status TEXT NOT NULL,
    reward REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (experiment_id, task_key)
        REFERENCES tasks(experiment_id, task_key)
);

CREATE TABLE IF NOT EXISTS checks (
    trajectory_id TEXT NOT NULL REFERENCES trajectories(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    score REAL,
    weight REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL,
    evidence TEXT,
    PRIMARY KEY (trajectory_id, name)
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
    rule_version TEXT,
    diagnosis_config_hash TEXT,
    judge_temperature REAL,
    judge_call_count INTEGER NOT NULL DEFAULT 0,
    trajectory_mode TEXT,
    evidence_validation_level TEXT,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    job_id TEXT NOT NULL,
    trial_id TEXT NOT NULL,
    trial_name TEXT NOT NULL,
    task_name TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    state TEXT NOT NULL,
    phase TEXT NOT NULL,
    retryable INTEGER NOT NULL DEFAULT 0,
    exception_type TEXT,
    exception_message TEXT,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    event_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(experiment_id, trial_id),
    UNIQUE(experiment_id, trial_name, attempt_number)
);

CREATE TABLE IF NOT EXISTS suite_runs (
    id TEXT PRIMARY KEY,
    suite_name TEXT NOT NULL,
    config_path TEXT,
    config_json TEXT NOT NULL,
    state TEXT NOT NULL,
    progress_json TEXT NOT NULL DEFAULT '{}',
    report_path TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suite_baselines (
    suite_name TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    version_name TEXT NOT NULL,
    promoted_at TEXT NOT NULL
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
    "agent_name": "TEXT",
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
    "source_schema_version": "TEXT",
    "canonical_schema_version": "TEXT",
    "adapter_version": "TEXT",
    "source_dataset": "TEXT",
    "source_instance_id": "TEXT",
}

DIAGNOSIS_MIGRATIONS = {
    "judge_thinking": "TEXT",
    "judge_max_input_tokens": "INTEGER",
    "judge_max_output_tokens": "INTEGER",
    "rule_version": "TEXT",
    "diagnosis_config_hash": "TEXT",
    "judge_temperature": "REAL",
    "judge_call_count": "INTEGER NOT NULL DEFAULT 0",
    "trajectory_mode": "TEXT",
    "evidence_validation_level": "TEXT",
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
    task_key = """
        CASE
            WHEN source_dataset IS NOT NULL AND source_dataset!=''
             AND source_instance_id IS NOT NULL AND source_instance_id!=''
            THEN CASE
                WHEN source_instance_id LIKE source_dataset || '/%'
                THEN source_instance_id
                ELSE source_dataset || '/' || source_instance_id
            END
            ELSE COALESCE(base_task_id, task_id)
        END
    """
    connection.execute(
        """
        INSERT OR IGNORE INTO tasks (
            experiment_id, task_key, source_dataset, source_instance_id,
            success_threshold, metadata_json
        )
        SELECT experiment_id, %s, source_dataset, source_instance_id, 1.0, '{}'
        FROM trajectories
        """
        % task_key
    )
    connection.execute(
        """
        INSERT INTO outcomes (
            trajectory_id, experiment_id, task_key, status, reward,
            metadata_json, created_at
        )
        SELECT id, experiment_id, %s, verdict, reward, '{}', ?
        FROM trajectories
        WHERE 1
        ON CONFLICT(trajectory_id) DO UPDATE SET
            status=excluded.status, reward=excluded.reward
        """
        % task_key,
        (utcnow(),),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO checks (
            trajectory_id, name, kind, status, score, weight, source, evidence
        )
        SELECT t.id, 'reward:aggregate', 'CODE',
               CASE WHEN t.reward>=1.0 THEN 'PASS' ELSE 'FAIL' END,
               t.reward, 1.0, 'migration', 'aggregate verifier reward'
        FROM trajectories t
        WHERE t.reward IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM checks c WHERE c.trajectory_id=t.id
          )
        """
    )
    connection.execute("PRAGMA user_version = %s" % DATABASE_SCHEMA_VERSION)
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
    for result_path in path.rglob("result.json"):
        trial_dir = result_path.parent
        agent_dir = trial_dir / "agent"
        if not any(agent_dir.rglob("trajectory.json")) and (
            agent_dir.is_dir() or read_json(result_path).get("trial_name")
        ):
            paths.add(result_path)
    return sorted(item for item in paths if "_retries" not in item.parts)


def sync_execution_events(
    connection: sqlite3.Connection, job_path: Path, experiment_id: str
) -> int:
    """Idempotently import Harbor's append-only lifecycle event stream."""
    ensure_experiment(connection, experiment_id)
    event_path = job_path / "execution-events.jsonl"
    if not event_path.exists():
        return 0
    events = []
    content = event_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            if line_number == len(lines) and not content.endswith("\n"):
                break
            raise ValueError(
                "Invalid Harbor execution event at line %s: %s" % (line_number, error)
            ) from error
        if event.get("event_version") != 1:
            raise ValueError(
                "Unsupported Harbor execution event version at line %s: %r"
                % (line_number, event.get("event_version"))
            )
        required = {
            "job_id",
            "trial_id",
            "trial_name",
            "task_name",
            "event",
            "state",
            "timestamp",
        }
        missing = sorted(required - event.keys())
        if missing:
            raise ValueError(
                "Harbor execution event at line %s is missing: %s"
                % (line_number, ", ".join(missing))
            )
        if event["event"] not in {
            "start",
            "environment-start",
            "agent-start",
            "agent-end",
            "verification-start",
            "heartbeat",
            "end",
            "cancel",
        }:
            raise ValueError("Unknown Harbor execution event: %s" % event["event"])
        if event["state"] not in {
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
            "INFRA_ERROR",
        }:
            raise ValueError("Unknown Harbor execution state: %s" % event["state"])
        try:
            datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(
                "Invalid Harbor event timestamp at line %s" % line_number
            ) from error
        events.append(event)

    grouped: Dict[str, Dict[str, Any]] = {}
    attempt_numbers: Dict[str, Dict[str, int]] = {}
    for event in events:
        trial_id = str(event["trial_id"])
        trial_name = str(event["trial_name"])
        attempts_for_trial = attempt_numbers.setdefault(trial_name, {})
        attempts_for_trial.setdefault(trial_id, len(attempts_for_trial) + 1)
        item = grouped.setdefault(
            trial_id,
            {
                "job_id": str(event["job_id"]),
                "trial_name": trial_name,
                "task_name": str(event["task_name"]),
                "attempt_number": attempts_for_trial[trial_id],
                "started_at": None,
                "event_count": 0,
            },
        )
        timestamp = str(event["timestamp"])
        item.update(
            state=str(event["state"]),
            phase=str(event["event"]),
            retryable=bool(event.get("retryable")),
            exception_type=event.get("exception_type"),
            exception_message=sanitize_value(event.get("exception_message")),
            updated_at=timestamp,
        )
        item["event_count"] += 1
        if event["event"] == "start" and item["started_at"] is None:
            item["started_at"] = timestamp
        item["finished_at"] = timestamp if event["event"] in {"end", "cancel"} else None

    for trial_id, item in grouped.items():
        attempt_id = hashlib.sha256(
            (experiment_id + "\0" + trial_id).encode("utf-8")
        ).hexdigest()[:32]
        connection.execute(
            """
            INSERT INTO attempts (
                id, experiment_id, job_id, trial_id, trial_name, task_name,
                attempt_number, state, phase, retryable, exception_type,
                exception_message, started_at, updated_at, finished_at, event_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(experiment_id, trial_id) DO UPDATE SET
                state=excluded.state, phase=excluded.phase,
                retryable=excluded.retryable,
                exception_type=excluded.exception_type,
                exception_message=excluded.exception_message,
                started_at=excluded.started_at, updated_at=excluded.updated_at,
                finished_at=excluded.finished_at, event_count=excluded.event_count
            """,
            (
                attempt_id,
                experiment_id,
                item["job_id"],
                trial_id,
                item["trial_name"],
                item["task_name"],
                item["attempt_number"],
                item["state"],
                item["phase"],
                int(item["retryable"]),
                item["exception_type"],
                item["exception_message"],
                item["started_at"],
                item["updated_at"],
                item["finished_at"],
                item["event_count"],
            ),
        )
    connection.commit()
    return len(grouped)


def execution_status(
    connection: sqlite3.Connection,
    experiment_id: str,
    lost_after_seconds: int = 90,
) -> Dict[str, Any]:
    rows = connection.execute(
        """
        SELECT * FROM attempts WHERE experiment_id=?
        ORDER BY trial_name, attempt_number
        """,
        (experiment_id,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    attempts = []
    for row in rows:
        item = dict(row)
        if item["state"] == "RUNNING":
            updated = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00"))
            if (now - updated).total_seconds() > lost_after_seconds:
                item["state"] = "LOST"
        attempts.append(item)
    latest = {}
    for item in attempts:
        latest[item["trial_name"]] = item
    states: Dict[str, int] = {}
    for item in latest.values():
        states[item["state"]] = states.get(item["state"], 0) + 1
    return {
        "experiment": experiment_id,
        "logical_trials": len(latest),
        "total_attempts": len(attempts),
        "retries": max(0, len(attempts) - len(latest)),
        "states": states,
        "attempts": attempts,
    }


def _number(value: Any) -> Optional[float]:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (dict, list, bool)):
            text = str(value).strip()
            if text:
                return text
    return None


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _task_key(metadata: Dict[str, Any]) -> str:
    dataset = str(metadata.get("source_dataset") or "").strip()
    instance = str(
        metadata.get("source_instance_id") or metadata.get("base_task_id") or ""
    ).strip()
    if not instance:
        raise ValueError("Imported trajectory is missing a logical task id")
    return (
        instance
        if not dataset or instance.startswith(dataset + "/")
        else "%s/%s" % (dataset, instance)
    )


AGENT_TIMEOUT_EXCEPTION = "AgentTimeoutError"
INFRA_OUTCOME_EXCEPTIONS = frozenset(
    {
        "EnvironmentStartTimeoutError",
        "VerifierTimeoutError",
        "HealthcheckError",
        "SandboxBuildFailedError",
        "NetworkConnectionError",
        "ApiInternalServerError",
        "ApiOverloadedError",
        "ApiConnectionClosedError",
        "ApiResponseStalledError",
        "RuntimeError",
    }
)


def map_trial_outcome(
    result: Dict[str, Any],
    checks: Optional[List[Dict[str, Any]]] = None,
    reward: Optional[float] = None,
    success_threshold: float = 1.0,
) -> str:
    exception = result.get("exception_info") or {}
    exception_type = str(exception.get("exception_type") or "")
    verifier = result.get("verifier_result") or {}
    if exception_type == AGENT_TIMEOUT_EXCEPTION:
        return "TIMEOUT"
    if exception_type in INFRA_OUTCOME_EXCEPTIONS:
        return "INFRA_ERROR"
    if checks:
        statuses = {item.get("status") for item in checks}
        if "FAIL" in statuses:
            return "FAIL"
        if statuses == {"PASS"}:
            return "PASS"
        return "UNKNOWN"
    if verifier and reward is not None:
        return "PASS" if reward >= success_threshold else "FAIL"
    if result.get("agent_result") is not None or result.get("agent_execution"):
        return "UNKNOWN"
    if exception:
        return "INFRA_ERROR"
    return "INCOMPLETE"


def _evaluation_data(
    verifier: Dict[str, Any], verdict: str, reward: Optional[float]
) -> Dict[str, Any]:
    threshold = _number(verifier.get("success_threshold"))
    threshold = 1.0 if threshold is None else threshold
    if not math.isfinite(threshold):
        raise ValueError("Verifier success_threshold must be finite")
    checks = []
    for name, value in _mapping(verifier.get("rewards")).items():
        score = _number(value)
        if score is None or not math.isfinite(score):
            continue
        checks.append(
            {
                "name": "reward:%s" % name,
                "kind": "CODE",
                "status": "PASS" if score >= threshold else "FAIL",
                "score": score,
                "weight": 1.0,
                "source": "verifier_result.rewards",
                "evidence": "%s=%s" % (name, score),
            }
        )
    raw_checks = verifier.get("checks") or []
    if not isinstance(raw_checks, list):
        raise ValueError("verifier_result.checks must be a list")
    for index, item in enumerate(raw_checks, 1):
        if not isinstance(item, dict):
            raise ValueError("Verifier check %s must be an object" % index)
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("Verifier check %s is missing name" % index)
        score = _number(item.get("score"))
        if score is not None and not math.isfinite(score):
            raise ValueError("Verifier check %s score must be finite" % name)
        status = str(item.get("status") or "").upper()
        if not status and score is not None:
            status = "PASS" if score >= threshold else "FAIL"
        if status not in {"PASS", "FAIL", "UNKNOWN"}:
            raise ValueError("Verifier check %s has invalid status" % name)
        weight = _number(item.get("weight"))
        weight = 1.0 if weight is None else weight
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("Verifier check %s weight must be positive" % name)
        kind = str(item.get("kind") or "CODE").upper()
        if kind not in {"CODE", "LLM", "HUMAN"}:
            raise ValueError("Verifier check %s has invalid kind" % name)
        checks.append(
            {
                "name": name,
                "kind": kind,
                "status": status,
                "score": score,
                "weight": weight,
                "source": str(
                    sanitize_value(item.get("source") or "verifier_result.checks")
                )[:500],
                "evidence": str(sanitize_value(item.get("evidence") or ""))[:4000]
                or None,
            }
        )
    if not checks and verdict in {"PASS", "FAIL"}:
        checks.append(
            {
                "name": "verdict",
                "kind": "CODE",
                "status": verdict,
                "score": reward,
                "weight": 1.0,
                "source": "result",
                "evidence": "verdict=%s" % verdict,
            }
        )
    names = [item["name"] for item in checks]
    if len(names) != len(set(names)):
        raise ValueError("Verifier check names must be unique")
    return {
        "success_threshold": threshold,
        "checks": checks,
        "outcome_metadata": sanitize_value(
            {"rewards": _mapping(verifier.get("rewards"))}
        ),
    }


def _agent_identity(
    data: Dict[str, Any], result: Optional[Dict[str, Any]] = None
) -> Dict[str, Optional[str]]:
    result = result or {}
    agent = data.get("agent")
    if isinstance(agent, str):
        name, version, model = agent, None, None
    else:
        agent = _mapping(agent)
        name = agent.get("name")
        version = agent.get("version")
        model = agent.get("model_name")
    result_agent = _mapping(result.get("agent_info"))
    model_info = _mapping(result_agent.get("model_info"))
    return {
        "agent_name": _first_text(name, result_agent.get("name")),
        "agent_version": _first_text(version, result_agent.get("version")),
        "model_name": _first_text(model, model_info.get("name")),
    }


def _bench_identity(
    data: Dict[str, Any],
    result: Optional[Dict[str, Any]],
    fallback: str,
) -> Dict[str, str]:
    result = result or {}
    extra = _mapping(data.get("extra"))
    result_extra = _mapping(result.get("extra"))
    source = _mapping(data.get("source"))
    result_config = _mapping(result.get("config"))
    result_task = _mapping(result_config.get("task"))
    dataset = _first_text(
        extra.get("source_dataset"),
        result_extra.get("source_dataset"),
        source.get("dataset"),
        result.get("source"),
        result_task.get("source"),
    )
    instance = _first_text(
        extra.get("source_instance_id"),
        result_extra.get("source_instance_id"),
        data.get("task_id"),
    )
    task_name = _first_text(result.get("task_name"), data.get("task_id"), fallback)
    task_name = task_name or fallback
    if not dataset and not instance and "/" in task_name:
        dataset, instance = task_name.split("/", 1)
    if not instance:
        instance = task_name
    return {
        "source_dataset": dataset,
        "source_instance_id": instance,
        "base_task_id": instance,
        "task_name": task_name,
    }


def _duration_seconds(value: Any) -> Optional[float]:
    if (
        not isinstance(value, dict)
        or not value.get("started_at")
        or not value.get("finished_at")
    ):
        return None
    try:
        start = datetime.fromisoformat(str(value["started_at"]).replace("Z", "+00:00"))
        finish = datetime.fromisoformat(
            str(value["finished_at"]).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    return max(0.0, (finish - start).total_seconds())


def _ctrf_checks(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    data = read_json(path)
    raw_tests = _mapping(data.get("results")).get("tests") or []
    if not isinstance(raw_tests, list):
        raise ValueError("CTRF results.tests must be a list")
    checks = []
    for index, item in enumerate(raw_tests, 1):
        if not isinstance(item, dict):
            raise ValueError("CTRF test %s must be an object" % index)
        name = str(item.get("name") or "test-%s" % index).strip()
        status = str(item.get("status") or "").lower()
        mapped = {
            "passed": "PASS",
            "failed": "FAIL",
            "skipped": "UNKNOWN",
            "pending": "UNKNOWN",
        }.get(status, "UNKNOWN")
        evidence = item.get("message") or item.get("trace") or status
        checks.append(
            {
                "name": "test:%s" % name,
                "kind": "CODE",
                "status": mapped,
                "score": 1.0 if mapped == "PASS" else (0.0 if mapped == "FAIL" else None),
                "weight": 1.0,
                "source": "verifier/ctrf.json",
                "evidence": str(sanitize_value(evidence))[:4000],
            }
        )
    names = [item["name"] for item in checks]
    if len(names) != len(set(names)):
        raise ValueError("CTRF test names must be unique")
    return checks


def _atif_metadata(
    raw_path: Path, data: Dict[str, Any], trial_dir: Optional[Path] = None
) -> Dict[str, Any]:
    trial_dir = trial_dir or raw_path.parent.parent
    result_path = trial_dir / "result.json"
    result = read_json(result_path) if result_path.exists() else {}
    exception = result.get("exception_info") or {}
    verifier = result.get("verifier_result") or {}
    rewards = _mapping(verifier.get("rewards"))
    values = [_number(value) for value in rewards.values()]
    values = [value for value in values if value is not None]
    reward = sum(values) / len(values) if values else None
    success_threshold = _number(verifier.get("success_threshold"))
    success_threshold = 1.0 if success_threshold is None else success_threshold
    evaluation = _evaluation_data(verifier, "UNKNOWN", reward)
    evaluation["checks"].extend(_ctrf_checks(trial_dir / "verifier" / "ctrf.json"))
    names = [item["name"] for item in evaluation["checks"]]
    if len(names) != len(set(names)):
        raise ValueError("Verifier and CTRF check names must be unique")
    verdict = map_trial_outcome(
        result, evaluation["checks"], reward, success_threshold
    )
    health = verdict if verdict in {"INFRA_ERROR", "INCOMPLETE"} else "VALID"

    identity = _agent_identity(data, result)
    bench = _bench_identity(data, result, trial_dir.name)
    trial_name = str(result.get("trial_name") or trial_dir.name)
    raw_events = sorted((trial_dir / "agent").rglob("session.jsonl"))
    final_logs = [
        trial_dir / "verifier" / "test-stdout.txt",
        trial_dir / "agent" / "dsh-minimal.txt",
    ]
    agent_result = result.get("agent_result") or {}
    return {
        "task_name": bench["task_name"],
        "base_task_id": bench["base_task_id"],
        "storage_task_id": "%s::%s" % (bench["task_name"], trial_name),
        "trajectory_id": str(result.get("id") or sha256_file(raw_path)[:16]),
        "trial_name": trial_name,
        "verdict": verdict,
        "health_status": health,
        "reward": reward,
        "raw_event_path": raw_events[0] if raw_events else None,
        "agent_name": identity["agent_name"],
        "agent_version": identity["agent_version"],
        "model_name": identity["model_name"],
        "source_dataset": bench["source_dataset"],
        "source_instance_id": bench["source_instance_id"],
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
        **evaluation,
    }


def _legacy_metadata(raw_path: Path, data: Dict[str, Any]) -> Dict[str, Any]:
    fallback = str(raw_path.parent.name or raw_path.stem)
    identity = _agent_identity(data)
    bench = _bench_identity(data, None, fallback)
    verdict = str(data.get("verdict") or "UNKNOWN").upper()
    stats = (data.get("info") or {}).get("model_stats") or {}
    reward = 1.0 if verdict == "PASS" else (0.0 if verdict == "FAIL" else None)
    return {
        "task_name": bench["task_name"],
        "base_task_id": bench["base_task_id"],
        "storage_task_id": bench["task_name"],
        "trajectory_id": sha256_file(raw_path)[:16],
        "trial_name": bench["task_name"],
        "verdict": verdict,
        "health_status": "INFRA_ERROR" if verdict == "INFRA_ERROR" else "VALID",
        "reward": reward,
        "raw_event_path": None,
        "agent_name": identity["agent_name"],
        "agent_version": identity["agent_version"],
        "model_name": identity["model_name"],
        "source_dataset": bench["source_dataset"],
        "source_instance_id": bench["source_instance_id"],
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
        **_evaluation_data({}, verdict, reward),
    }


def _save_evaluation_records(
    connection: sqlite3.Connection,
    experiment_id: str,
    trajectory_id: str,
    metadata: Dict[str, Any],
) -> None:
    task_key = _task_key(metadata)
    connection.execute(
        """
        INSERT INTO tasks (
            experiment_id, task_key, source_dataset, source_instance_id,
            success_threshold, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(experiment_id, task_key) DO UPDATE SET
            source_dataset=excluded.source_dataset,
            source_instance_id=excluded.source_instance_id,
            success_threshold=excluded.success_threshold,
            metadata_json=excluded.metadata_json
        """,
        (
            experiment_id,
            task_key,
            metadata.get("source_dataset"),
            metadata.get("source_instance_id"),
            metadata["success_threshold"],
            json.dumps({"task_name": metadata.get("task_name")}, ensure_ascii=False),
        ),
    )
    connection.execute(
        """
        INSERT INTO outcomes (
            trajectory_id, experiment_id, task_key, status, reward,
            metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trajectory_id) DO UPDATE SET
            experiment_id=excluded.experiment_id, task_key=excluded.task_key,
            status=excluded.status, reward=excluded.reward,
            metadata_json=excluded.metadata_json, created_at=excluded.created_at
        """,
        (
            trajectory_id,
            experiment_id,
            task_key,
            metadata["verdict"],
            metadata["reward"],
            json.dumps(metadata["outcome_metadata"], ensure_ascii=False),
            utcnow(),
        ),
    )
    connection.execute("DELETE FROM checks WHERE trajectory_id=?", (trajectory_id,))
    connection.executemany(
        """
        INSERT INTO checks (
            trajectory_id, name, kind, status, score, weight, source, evidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                trajectory_id,
                item["name"],
                item["kind"],
                item["status"],
                item["score"],
                item["weight"],
                item["source"],
                item["evidence"],
            )
            for item in metadata["checks"]
        ],
    )


def import_run(
    connection: sqlite3.Connection,
    path: Path,
    experiment_id: str,
    agent_model: Optional[str] = None,
    agent_name: Optional[str] = None,
    model_name: Optional[str] = None,
) -> List[str]:
    ensure_experiment(connection, experiment_id, agent_model)
    trajectory_ids = []
    for raw_path in _trajectory_paths(path):
        data = read_json(raw_path)
        is_harbor_result = bool(raw_path.name == "result.json" and data.get("trial_name"))
        if is_harbor_result:
            steps = []
            source_schema_version = "harbor-result-v1"
            metadata = _atif_metadata(raw_path, {}, raw_path.parent)
            is_atif = False
        else:
            steps = normalize_trajectory(data)
            source_schema_version = validate_trajectory_schema(data)
            is_atif = source_schema_version is not None
            metadata = (
                _atif_metadata(raw_path, data)
                if is_atif
                else _legacy_metadata(raw_path, data)
            )
        configured_agent = _mapping(_mapping(data.get("config")).get("agent"))
        actual_agent = _first_text(configured_agent.get("name"), metadata["agent_name"])
        actual_model = _first_text(
            configured_agent.get("model_name"), metadata["model_name"]
        )
        if agent_name is not None and actual_agent != agent_name:
            continue
        if model_name is not None and actual_model not in {
            model_name,
            model_name.rsplit("/", 1)[-1],
        }:
            continue
        resolved_raw_path = str(raw_path.resolve())
        existing = connection.execute(
            "SELECT * FROM trajectories WHERE experiment_id=? AND task_id=?",
            (experiment_id, metadata["storage_task_id"]),
        ).fetchone()
        path_existing = connection.execute(
            """
            SELECT * FROM trajectories
            WHERE experiment_id=? AND raw_path=?
            ORDER BY health_status='INCOMPLETE', finished_at DESC
            LIMIT 1
            """,
            (experiment_id, resolved_raw_path),
        ).fetchone()
        if not existing:
            existing = path_existing
        if existing:
            connection.execute(
                "DELETE FROM trajectories WHERE experiment_id=? AND raw_path=? AND id!=?",
                (experiment_id, resolved_raw_path, existing["id"]),
            )
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
        same_trace = bool(
            existing
            and existing["raw_sha256"] == raw_digest
            and existing["task_id"] == metadata["storage_task_id"]
            and existing["verdict"] == metadata["verdict"]
            and existing["health_status"] == metadata["health_status"]
            and existing["reward"] == metadata["reward"]
        )
        if existing and existing["task_id"] != metadata["storage_task_id"]:
            connection.execute(
                "UPDATE trajectories SET task_id=? WHERE id=?",
                (metadata["storage_task_id"], trajectory_id),
            )
        connection.execute(
            """
            INSERT INTO trajectories (
                id, experiment_id, task_id, verdict, raw_path, raw_sha256,
                final_patch_path, final_log_path, cost, api_calls, base_task_id,
                trial_name, health_status, reward, raw_event_path, raw_event_sha256,
                agent_version, agent_name, model_name, started_at, finished_at,
                input_tokens, cache_tokens, output_tokens,
                environment_setup_seconds, agent_setup_seconds,
                agent_execution_seconds, verifier_seconds, source_schema_version,
                canonical_schema_version, adapter_version, source_dataset,
                source_instance_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(experiment_id, task_id) DO UPDATE SET
                verdict=excluded.verdict, raw_path=excluded.raw_path,
                raw_sha256=excluded.raw_sha256,
                final_patch_path=excluded.final_patch_path,
                final_log_path=excluded.final_log_path, cost=excluded.cost,
                api_calls=excluded.api_calls, base_task_id=excluded.base_task_id,
                trial_name=excluded.trial_name, health_status=excluded.health_status,
                reward=excluded.reward, raw_event_path=excluded.raw_event_path,
                raw_event_sha256=excluded.raw_event_sha256,
                agent_version=excluded.agent_version, agent_name=excluded.agent_name,
                model_name=excluded.model_name,
                started_at=excluded.started_at, finished_at=excluded.finished_at,
                input_tokens=excluded.input_tokens, cache_tokens=excluded.cache_tokens,
                output_tokens=excluded.output_tokens,
                environment_setup_seconds=excluded.environment_setup_seconds,
                agent_setup_seconds=excluded.agent_setup_seconds,
                agent_execution_seconds=excluded.agent_execution_seconds,
                verifier_seconds=excluded.verifier_seconds,
                source_schema_version=excluded.source_schema_version,
                canonical_schema_version=excluded.canonical_schema_version,
                adapter_version=excluded.adapter_version,
                source_dataset=excluded.source_dataset,
                source_instance_id=excluded.source_instance_id
            """,
            (
                trajectory_id,
                experiment_id,
                metadata["storage_task_id"],
                metadata["verdict"],
                resolved_raw_path,
                raw_digest,
                str(final_patch.resolve()) if final_patch.exists() else None,
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
                metadata["agent_name"],
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
                source_schema_version or "legacy",
                CANONICAL_SCHEMA_VERSION,
                ADAPTER_VERSION,
                metadata["source_dataset"],
                metadata["source_instance_id"],
            ),
        )
        _save_evaluation_records(connection, experiment_id, trajectory_id, metadata)
        if same_trace:
            connection.commit()
            trajectory_ids.append(trajectory_id)
            continue
        connection.execute("DELETE FROM steps WHERE trajectory_id=?", (trajectory_id,))
        connection.execute(
            "DELETE FROM diagnoses WHERE trajectory_id=?", (trajectory_id,)
        )
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
                    str(sanitize_value(step["content"]))[:12000],
                    sanitize_value(step["command"]),
                    step["test_status"],
                    step.get("tool_name"),
                    json.dumps(
                        sanitize_value(step.get("tool_arguments")), ensure_ascii=False
                    ),
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


def get_diagnosis(
    connection: sqlite3.Connection, trajectory_id: str
) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM diagnoses WHERE trajectory_id=?", (trajectory_id,)
    ).fetchone()


def diagnosable_trajectories(
    connection: sqlite3.Connection, experiment_id: str
) -> List[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM trajectories
        WHERE experiment_id=?
          AND verdict IN (
              'FAIL', 'TIMEOUT', 'INFRA_ERROR', 'UNKNOWN', 'INCOMPLETE'
          )
        ORDER BY base_task_id, trial_name
        """,
        (experiment_id,),
    ).fetchall()


def save_diagnosis(
    connection: sqlite3.Connection, trajectory_id: str, report: Dict[str, Any]
) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO diagnoses (
            trajectory_id, status, responsibility, category_code, category_name,
            root_cause_step, component, summary, confidence, decision_source,
            matched_rule, judge_model, prompt_version, judge_input_tokens,
            judge_output_tokens, judge_latency_seconds, judge_thinking,
            judge_max_input_tokens, judge_max_output_tokens, diagnosis_error,
            rule_version, diagnosis_config_hash, judge_temperature,
            judge_call_count, trajectory_mode, evidence_validation_level,
            report_json, created_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            trajectory_id,
            report["status"],
            report.get("responsibility"),
            report.get("category_code"),
            report.get("category_name"),
            report.get("root_cause_step"),
            report.get("component"),
            report.get("summary") or "",
            report.get("confidence"),
            report.get("decision_source"),
            report.get("matched_rule"),
            report.get("judge_model"),
            report.get("prompt_version") or "unknown",
            int(report.get("judge_input_tokens") or 0),
            int(report.get("judge_output_tokens") or 0),
            float(report.get("judge_latency_seconds") or 0),
            report.get("judge_thinking"),
            report.get("max_input_tokens"),
            report.get("max_output_tokens"),
            report.get("diagnosis_error"),
            report.get("rule_version"),
            report.get("diagnosis_config_hash"),
            report.get("judge_temperature"),
            int(report.get("judge_call_count") or 0),
            report.get("trajectory_mode"),
            report.get("evidence_validation_level"),
            json.dumps(report, ensure_ascii=False),
            utcnow(),
        ),
    )
    connection.commit()
