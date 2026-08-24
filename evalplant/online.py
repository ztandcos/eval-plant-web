import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

from .db import connect, enqueue_attribution, import_run


def ingest_payload(
    db_path: Path, store: Path, payload: Dict[str, Any]
) -> Dict[str, Any]:
    experiment = payload.get("experiment")
    trajectory = payload.get("trajectory")
    if not isinstance(experiment, str) or not experiment.strip():
        raise ValueError("experiment is required")
    if not isinstance(trajectory, dict) or not str(
        trajectory.get("schema_version") or ""
    ).startswith("ATIF-"):
        raise ValueError("trajectory must be an ATIF JSON object")

    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    trial_dir = store / digest / "trial"
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "trajectory.json").write_text(
        json.dumps(trajectory, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    raw_events = payload.get("raw_events")
    if isinstance(raw_events, str):
        session_dir = (
            agent_dir / "dsh-sessions" / str(trajectory.get("session_id") or "session")
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "session.jsonl").write_text(raw_events, encoding="utf-8")
    verifier_log = payload.get("verifier_log")
    if isinstance(verifier_log, str):
        verifier_dir = trial_dir / "verifier"
        verifier_dir.mkdir(exist_ok=True)
        (verifier_dir / "test-stdout.txt").write_text(verifier_log, encoding="utf-8")

    result = payload.get("result")
    if not isinstance(result, dict):
        result = {
            "id": digest[:16],
            "task_name": str(payload.get("task_id") or digest[:16]),
            "trial_name": "online__%s" % digest[:8],
            "agent_result": {"metadata": {"online": True}},
        }
    (trial_dir / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    connection = connect(db_path)
    try:
        trajectory_id = import_run(connection, trial_dir, experiment)[0]
        queued = enqueue_attribution(connection, trajectory_id)
        row = connection.execute(
            "SELECT health_status, verdict FROM trajectories WHERE id=?",
            (trajectory_id,),
        ).fetchone()
        return {
            "trajectory_id": trajectory_id,
            "health_status": row["health_status"],
            "verdict": row["verdict"],
            "attribution_queued": queued,
        }
    finally:
        connection.close()


def serve(db_path: Path, store: Path, host: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/ingest":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 50 * 1024 * 1024:
                    raise ValueError("request body must be between 1 byte and 50 MiB")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("request body must be a JSON object")
                response = ingest_payload(db_path, store, payload)
                status = 201
            except (OSError, ValueError, json.JSONDecodeError) as error:
                response, status = {"error": str(error)}, 400
            body = json.dumps(response, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()
