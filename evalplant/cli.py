import argparse
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .db import (
    connect,
    diagnosable_trajectories,
    ensure_experiment,
    execution_status,
    get_diagnosis,
    get_steps,
    get_trajectory,
    import_run,
    save_diagnosis,
    sync_execution_events,
)
from .evaluation import evaluate, read_jsonl, read_predictions
from .judge import DEFAULT_MAX_INPUT_TOKENS, analyze_trajectory, failed_diagnosis
from .metrics import compare_experiments, report

console = Console()
error_console = Console(stderr=True)

TERMINAL_STATES = {
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "INFRA_ERROR",
    "LOST",
}


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _read_optional(value: Optional[str]) -> str:
    return _path(value).read_text(encoding="utf-8", errors="replace") if value else ""


def _default_experiment(path: Path) -> str:
    if path.name == "cases" and path.parent.name not in {"", "."}:
        return path.parent.name
    return path.name


def _display_task(row: Any) -> str:
    dataset = row["source_dataset"]
    instance = row["source_instance_id"] or row["base_task_id"]
    if dataset and instance:
        instance_text = str(instance)
        prefix = "%s/" % dataset
        if instance_text.startswith(prefix):
            return instance_text
        return "%s/%s" % (dataset, instance_text)
    return str(instance or row["base_task_id"] or row["trial_name"])


def _looks_live(path: Optional[Path]) -> bool:
    return bool(path and path.is_dir() and (path / "execution-events.jsonl").exists())


def _job_complete(status: Dict[str, Any]) -> bool:
    if not status["logical_trials"]:
        return False
    latest: Dict[str, Dict[str, Any]] = {}
    for item in status["attempts"]:
        latest[item["trial_name"]] = item
    return all(item["state"] in TERMINAL_STATES for item in latest.values())


def _import_available(
    connection: sqlite3.Connection, path: Path, experiment: str
) -> List[str]:
    try:
        return import_run(connection, path, experiment)
    except ValueError as error:
        if "No trajectory JSON files found" in str(error):
            return []
        raise


def command_import(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    ids = import_run(connection, _path(args.path), args.experiment, args.agent_model)
    job_path = _path(args.path)
    if job_path.is_dir():
        sync_execution_events(connection, job_path, args.experiment)
    console.print(
        "Imported [bold green]%s[/bold green] trajectory(s): %s"
        % (len(ids), ", ".join(ids))
    )


def command_observe(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    if args.lost_after_seconds <= 0:
        raise ValueError("--lost-after-seconds must be positive")
    job_path = _path(args.path)
    imported = sync_execution_events(connection, job_path, args.experiment)
    result = execution_status(connection, args.experiment, args.lost_after_seconds)
    if args.output:
        output = _path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    table = Table("Trial", "Attempt", "State", "Phase", "Updated", "Exception")
    for item in result["attempts"]:
        table.add_row(
            item["trial_name"],
            str(item["attempt_number"]),
            item["state"],
            item["phase"],
            item["updated_at"],
            item["exception_type"] or "",
        )
    summary = "logical=%s attempts=%s retries=%s states=%s synced=%s" % (
        result["logical_trials"],
        result["total_attempts"],
        result["retries"],
        json.dumps(result["states"], ensure_ascii=False, sort_keys=True),
        imported,
    )
    console.print(Panel(table, title=summary))


def command_inspect(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    trajectory = get_trajectory(connection, args.trajectory)
    diagnosis = get_diagnosis(connection, args.trajectory)
    lines = [
        "Agent: %s" % (trajectory["agent_name"] or "n/a"),
        "Agent version: %s" % (trajectory["agent_version"] or "n/a"),
        "Model: %s" % (trajectory["model_name"] or "n/a"),
        "Dataset: %s" % (trajectory["source_dataset"] or "n/a"),
        "Task: %s" % (trajectory["source_instance_id"] or trajectory["base_task_id"]),
        "Trial: %s" % trajectory["trial_name"],
        "Health: %s" % trajectory["health_status"],
        "Verdict: %s" % trajectory["verdict"],
        "Reward: %s" % trajectory["reward"],
    ]
    evidence_steps = set()
    root_step = None
    if diagnosis:
        lines += [
            "Diagnosis: %s" % diagnosis["status"],
            "Responsibility: %s" % (diagnosis["responsibility"] or "UNDETERMINED"),
            "Category: %s %s"
            % (diagnosis["category_code"] or "", diagnosis["category_name"] or ""),
            "Component: %s" % (diagnosis["component"] or "n/a"),
            "Confidence: %s" % (diagnosis["confidence"] or "n/a"),
            "Summary: %s" % diagnosis["summary"],
        ]
        root_step = diagnosis["root_cause_step"]
        detail = json.loads(diagnosis["report_json"])
        evidence_steps = {
            item["step_id"]
            for item in detail.get("evidence", [])
            if item.get("step_id") is not None
        }
    console.print(Panel("\n".join(lines), title="Trajectory %s" % trajectory["id"]))

    table = Table("Step", "Role", "Type", "Command / content", "Test")
    for step in get_steps(connection, args.trajectory):
        marker = (
            "★"
            if step["step_index"] == root_step
            else ("•" if step["step_index"] in evidence_steps else "")
        )
        text = step["command"] or step["content_preview"].replace("\n", " ")
        table.add_row(
            "%s %s" % (step["step_index"], marker),
            step["role"],
            step["action_type"],
            Text(text[:120]),
            step["test_status"] or "",
        )
    console.print(table)
    checks = connection.execute(
        "SELECT * FROM checks WHERE trajectory_id=? ORDER BY name",
        (args.trajectory,),
    ).fetchall()
    if checks:
        check_table = Table("Check", "Kind", "Status", "Score", "Source")
        for item in checks:
            check_table.add_row(
                item["name"],
                item["kind"],
                item["status"],
                "n/a" if item["score"] is None else str(item["score"]),
                item["source"],
            )
        console.print(check_table)


def _diagnose_trajectory(
    connection: sqlite3.Connection,
    trajectory: sqlite3.Row,
    model: str,
    max_input_tokens: int,
) -> Dict[str, Any]:
    try:
        result = analyze_trajectory(
            Path(trajectory["raw_path"]),
            trajectory["verdict"],
            trajectory["health_status"],
            _read_optional(trajectory["final_log_path"]),
            model,
            max_input_tokens,
        )
    except Exception as error:
        result = failed_diagnosis(error, model, max_input_tokens)
    save_diagnosis(connection, trajectory["id"], result)
    return result


def command_analyze(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    trajectories = diagnosable_trajectories(connection, args.experiment)
    if args.trajectory:
        trajectories = [row for row in trajectories if row["id"] == args.trajectory]
    if not trajectories:
        raise ValueError(
            "No diagnosable trajectories in experiment %s" % args.experiment
        )
    connection.execute(
        "UPDATE experiments SET judge_model=? WHERE id=?", (args.model, args.experiment)
    )
    connection.commit()
    for trajectory in trajectories:
        if get_diagnosis(connection, trajectory["id"]) and not args.force:
            console.print(
                "[dim]%s already diagnosed[/dim]" % trajectory["base_task_id"]
            )
            continue
        result = _diagnose_trajectory(
            connection, trajectory, args.model, args.max_input_tokens
        )
        color = "green" if result["status"] == "ATTRIBUTED" else "yellow"
        console.print(
            "[%s]%s[/%s] status=%s responsibility=%s category=%s component=%s"
            % (
                color,
                trajectory["base_task_id"],
                color,
                result["status"],
                result.get("responsibility") or "n/a",
                result.get("category_code") or "n/a",
                result.get("component") or "n/a",
            )
        )
        if result.get("diagnosis_error"):
            error_console.print(
                "[red]Diagnosis failed:[/red] %s" % result["diagnosis_error"]
            )


def _number(value: Optional[float], suffix: str = "") -> str:
    return "n/a" if value is None else "%s%s" % (f"{value:,.2f}", suffix)


def _report_payload(connection: sqlite3.Connection, experiment: str) -> Dict[str, Any]:
    result = report(connection, experiment)
    diagnoses = connection.execute(
        """
        SELECT t.id, t.base_task_id, t.trial_name, t.verdict, t.health_status,
               t.agent_name, t.agent_version, t.model_name, t.source_dataset,
               t.source_instance_id, t.source_schema_version,
               t.canonical_schema_version, t.adapter_version, d.report_json
        FROM trajectories t JOIN diagnoses d ON d.trajectory_id=t.id
        WHERE t.experiment_id=? ORDER BY t.base_task_id, t.trial_name
        """,
        (experiment,),
    ).fetchall()
    trials = connection.execute(
        """
        SELECT t.id, t.base_task_id, t.trial_name, t.agent_name, t.model_name,
               t.cost, t.agent_execution_seconds, o.task_key, o.status, o.reward
        FROM trajectories t JOIN outcomes o ON o.trajectory_id=t.id
        WHERE t.experiment_id=? ORDER BY o.task_key, t.trial_name
        """,
        (experiment,),
    ).fetchall()
    check_rows = connection.execute(
        """
        SELECT c.* FROM checks c JOIN trajectories t ON t.id=c.trajectory_id
        WHERE t.experiment_id=? ORDER BY c.trajectory_id, c.name
        """,
        (experiment,),
    ).fetchall()
    checks: Dict[str, List[Dict[str, Any]]] = {}
    for row in check_rows:
        checks.setdefault(row["trajectory_id"], []).append(
            {
                "name": row["name"],
                "kind": row["kind"],
                "status": row["status"],
                "score": row["score"],
                "weight": row["weight"],
                "source": row["source"],
                "evidence": row["evidence"],
            }
        )
    return {
        "statistics": result,
        "trials": [
            {
                "trajectory_id": row["id"],
                "task_key": row["task_key"],
                "task_id": row["base_task_id"],
                "trial_name": row["trial_name"],
                "agent": row["agent_name"],
                "model": row["model_name"],
                "outcome": row["status"],
                "reward": row["reward"],
                "cost": row["cost"],
                "agent_execution_seconds": row["agent_execution_seconds"],
                "checks": checks.get(row["id"], []),
            }
            for row in trials
        ],
        "diagnoses": [
            {
                "trajectory_id": row["id"],
                "case_id": row["trial_name"],
                "task_id": row["base_task_id"],
                "agent": row["agent_name"],
                "model": row["model_name"],
                "dataset": row["source_dataset"],
                "instance_id": row["source_instance_id"],
                "verdict": row["verdict"],
                "health_status": row["health_status"],
                "source_schema_version": row["source_schema_version"],
                "canonical_schema_version": row["canonical_schema_version"],
                "adapter_version": row["adapter_version"],
                "diagnosis": json.loads(row["report_json"]),
            }
            for row in diagnoses
        ],
    }


def _write_report_json(payload: Dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output


def _refresh_report(
    connection: sqlite3.Connection, experiment: str, output: Path
) -> Optional[Dict[str, Any]]:
    try:
        payload = _report_payload(connection, experiment)
    except ValueError:
        return None
    _write_report_json(payload, output)
    return payload


def _print_report_tables(result: Dict[str, Any], experiment: str) -> None:
    if not result.get("diagnoses_comparable", True):
        error_console.print(
            "[bold yellow]Warning:[/bold yellow] diagnoses use multiple "
            "configurations; "
            "do not compare or aggregate them as one accuracy result."
        )
    table = Table("Metric", "Value")
    for label, value in (
        ("Total tasks", result["total_tasks"]),
        ("Total trials", result["total_trials"]),
        ("Successful tasks", result["successful_tasks"]),
        ("Failed tasks", result["failed_tasks"]),
        ("Successful trials", result["successful_trials"]),
        ("Failed trials", result["failed_trials"]),
        ("Trial pass rate", _number(result["trial_pass_rate"])),
        ("Task any-pass rate", _number(result["task_any_pass_rate"])),
        ("Task all-pass rate", _number(result["task_all_pass_rate"])),
        (
            "Check statuses",
            json.dumps(result["check_statuses"], ensure_ascii=False, sort_keys=True),
        ),
        ("Weighted check pass rate", _number(result["weighted_check_pass_rate"])),
        (
            "Verdicts",
            json.dumps(result["verdicts"], ensure_ascii=False, sort_keys=True),
        ),
        (
            "Diagnosis statuses",
            json.dumps(
                result["diagnosis_statuses"], ensure_ascii=False, sort_keys=True
            ),
        ),
        (
            "Responsibilities",
            json.dumps(result["responsibilities"], ensure_ascii=False, sort_keys=True),
        ),
        (
            "Harness layers",
            json.dumps(result["harness_layers"], ensure_ascii=False, sort_keys=True),
        ),
        (
            "LLM categories",
            json.dumps(result["llm_categories"], ensure_ascii=False, sort_keys=True),
        ),
        (
            "Confidence",
            json.dumps(result["confidence"], ensure_ascii=False, sort_keys=True),
        ),
        (
            "Decision source",
            json.dumps(result["decision_sources"], ensure_ascii=False, sort_keys=True),
        ),
        (
            "Components",
            json.dumps(result["components"], ensure_ascii=False, sort_keys=True),
        ),
        (
            "Diagnosis configs",
            json.dumps(
                result["diagnosis_config_hashes"], ensure_ascii=False, sort_keys=True
            ),
        ),
        ("Diagnoses comparable", result["diagnoses_comparable"]),
        (
            "Trajectory modes",
            json.dumps(result["trajectory_modes"], ensure_ascii=False, sort_keys=True),
        ),
        (
            "Recommended actions",
            json.dumps(
                result["recommended_actions"], ensure_ascii=False, sort_keys=True
            ),
        ),
        ("Average input tokens", _number(result["average_input_tokens"])),
        ("Average cache tokens", _number(result["average_cache_tokens"])),
        ("Average output tokens", _number(result["average_output_tokens"])),
        ("Average run cost", _number(result["average_cost"], " USD")),
        ("Average agent time", _number(result["average_agent_seconds"], "s")),
        ("Average verifier time", _number(result["average_verifier_seconds"], "s")),
        (
            "Diagnosis input/output tokens",
            "%s / %s"
            % (result["diagnosis_input_tokens"], result["diagnosis_output_tokens"]),
        ),
        ("Diagnosis latency", _number(result["diagnosis_latency_seconds"], "s")),
    ):
        table.add_row(str(label), str(value))
    console.print(Panel(table, title=experiment))

    grouped = Table("Group", "Total", "Pass", "Fail", "Harness", "LLM")
    for prefix, rows in (
        ("agent", result.get("by_agent") or {}),
        ("dataset", result.get("by_dataset") or {}),
        ("model", result["by_model"]),
        ("agent-version", result["by_agent_version"]),
    ):
        for name, values in rows.items():
            grouped.add_row(
                "%s:%s" % (prefix, name),
                str(values["total"]),
                str(values["passed"]),
                str(values["failed"]),
                str(values["harness"]),
                str(values["llm"]),
            )
    console.print(Panel(grouped, title="Breakdown"))


def command_report(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    payload = _report_payload(connection, args.experiment)
    if args.output:
        output = _write_report_json(payload, _path(args.output))
        console.print("Exported report to [bold green]%s[/bold green]" % output)
    _print_report_tables(payload["statistics"], args.experiment)


def command_compare(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    result = compare_experiments(
        connection,
        args.baseline,
        args.candidate,
        args.k,
        args.max_cost_increase,
    )
    if args.output:
        output = _write_report_json(result, _path(args.output))
        console.print("Exported comparison to [bold green]%s[/bold green]" % output)
    table = Table("Metric", "Baseline", "Candidate", "Delta")
    for key, label in (
        ("pass_at_k", "pass@%s" % args.k),
        ("pass_power_k", "pass^%s" % args.k),
        ("average_cost", "Average cost"),
        ("average_agent_seconds", "Average agent seconds"),
    ):
        before = result["baseline_metrics"][key]
        after = result["candidate_metrics"][key]
        delta_key = {
            "average_cost": "cost_relative",
            "average_agent_seconds": "agent_seconds_relative",
        }.get(key, key)
        table.add_row(
            label, _number(before), _number(after), _number(result["deltas"][delta_key])
        )
    console.print(
        Panel(
            table,
            title="%s → %s | shared=%s eligible=%s | gate=%s"
            % (
                args.baseline,
                args.candidate,
                result["shared_tasks"],
                result["eligible_tasks"],
                result["ship_gate"]["status"],
            ),
        )
    )
    changes = result["changes"]
    console.print(
        "improved=%s regressed=%s unchanged=%s reasons=%s"
        % (
            changes["improved"],
            changes["regressed"],
            changes["unchanged"],
            "; ".join(result["ship_gate"]["reasons"]) or "none",
        )
    )


def _print_ok(row: sqlite3.Row) -> None:
    console.print("[bold green]OK[/bold green]    %s  PASS" % _display_task(row))


def _print_fail(row: sqlite3.Row, diagnosis: Dict[str, Any], report_path: Path) -> None:
    step = diagnosis.get("root_cause_step")
    console.print(
        "[bold red]%s[/bold red]  %s  → %s  %s/%s  step=%s"
        % (
            row["verdict"],
            _display_task(row),
            diagnosis.get("status") or "n/a",
            diagnosis.get("responsibility") or "n/a",
            diagnosis.get("category_code") or "n/a",
            step if step is not None else "n/a",
        )
    )
    console.print("      report: %s" % report_path)


def _ready_to_diagnose(
    row: sqlite3.Row, attempt: Optional[Dict[str, Any]], live: bool
) -> bool:
    if row["verdict"] == "PASS":
        return False
    if row["verdict"] in {"FAIL", "TIMEOUT", "INFRA_ERROR"}:
        return True
    if not live:
        return True
    return bool(attempt and attempt["state"] in TERMINAL_STATES)


def _process_batch(
    connection: sqlite3.Connection,
    experiment: str,
    model: str,
    max_input_tokens: int,
    force: bool,
    output: Path,
    seen: Set[str],
    live: bool,
    lost_after_seconds: int,
) -> None:
    trajectories = connection.execute(
        """
        SELECT * FROM trajectories WHERE experiment_id=?
        ORDER BY base_task_id, trial_name
        """,
        (experiment,),
    ).fetchall()
    attempts: Dict[str, Dict[str, Any]] = {}
    status = None
    if live:
        status = execution_status(connection, experiment, lost_after_seconds)
        for item in status["attempts"]:
            attempts[item["trial_name"]] = item
    refreshed = False
    for row in trajectories:
        if row["id"] in seen and not force:
            continue
        if row["verdict"] == "PASS":
            if row["id"] not in seen:
                _print_ok(row)
                seen.add(row["id"])
            continue
        if not _ready_to_diagnose(row, attempts.get(row["trial_name"]), live):
            continue
        existing = get_diagnosis(connection, row["id"])
        if existing and not (force and row["id"] not in seen):
            diagnosis = dict(existing)
        else:
            diagnosis = _diagnose_trajectory(connection, row, model, max_input_tokens)
            refreshed = True
        if row["id"] not in seen:
            _print_fail(row, diagnosis, output)
            seen.add(row["id"])
            refreshed = True
    if live and status is not None:
        known = {row["trial_name"] for row in trajectories}
        for trial_name, item in attempts.items():
            key = "lost:%s" % trial_name
            if item["state"] == "LOST" and trial_name not in known and key not in seen:
                console.print("[yellow]LOST[/yellow]  %s" % item["task_name"])
                seen.add(key)
    if refreshed:
        _refresh_report(connection, experiment, output)


def run_pipeline(
    connection: sqlite3.Connection,
    path: Optional[Path],
    experiment: str,
    model: str,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    force: bool = False,
    once: bool = False,
    output: Optional[Path] = None,
    gold: Optional[Path] = None,
    poll_seconds: float = 3.0,
    lost_after_seconds: int = 90,
    inject: Optional[Callable[[int], None]] = None,
    max_polls: Optional[int] = None,
) -> Dict[str, Any]:
    ensure_experiment(connection, experiment)
    connection.execute(
        "UPDATE experiments SET judge_model=? WHERE id=?", (model, experiment)
    )
    connection.commit()
    if output is None:
        output = Path("data") / ("%s-report.json" % experiment)
    output = Path(output)
    live = (not once) and _looks_live(path)
    if live and path is not None:
        sync_execution_events(connection, path, experiment)
        if _job_complete(execution_status(connection, experiment, lost_after_seconds)):
            live = False
    if path is not None and not live:
        imported = _import_available(connection, path, experiment)
        existing = connection.execute(
            "SELECT COUNT(*) FROM trajectories WHERE experiment_id=?",
            (experiment,),
        ).fetchone()[0]
        if not imported and not existing:
            raise ValueError("No trajectory JSON files found under %s" % path)
    seen: Set[str] = set()
    tick = 0
    try:
        while True:
            if inject is not None:
                inject(tick)
            if path is not None and live:
                sync_execution_events(connection, path, experiment)
                _import_available(connection, path, experiment)
            elif path is not None and tick == 0:
                _import_available(connection, path, experiment)
            _process_batch(
                connection,
                experiment,
                model,
                max_input_tokens,
                force,
                output,
                seen,
                live,
                lost_after_seconds,
            )
            tick += 1
            if not live:
                break
            status = execution_status(connection, experiment, lost_after_seconds)
            if _job_complete(status):
                _process_batch(
                    connection,
                    experiment,
                    model,
                    max_input_tokens,
                    force,
                    output,
                    seen,
                    False,
                    lost_after_seconds,
                )
                break
            if max_polls is not None and tick >= max_polls:
                break
            time.sleep(max(0.0, poll_seconds))
    except KeyboardInterrupt:
        console.print("[dim]Stopped. Latest report kept at %s[/dim]" % output)
    payload = _refresh_report(connection, experiment, output)
    if payload is None:
        raise ValueError("No trajectories imported for experiment %s" % experiment)
    _print_report_tables(payload["statistics"], experiment)
    if gold is not None:
        gold_result = evaluate(read_jsonl(gold), read_predictions(output))
        table = Table("Metric", "Value")
        for key in (
            "paired_cases",
            "responsibility_accuracy",
            "category_accuracy",
            "root_step_exact_accuracy",
            "coverage",
        ):
            if key in gold_result:
                table.add_row(key, str(gold_result[key]))
        console.print(Panel(table, title="Gold evaluation"))
    console.print("Report: [bold green]%s[/bold green]" % output)
    return payload


def command_run(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    if args.poll_seconds < 0:
        raise ValueError("--poll-seconds must be >= 0")
    if args.lost_after_seconds <= 0:
        raise ValueError("--lost-after-seconds must be positive")
    raw = Path(args.path).expanduser()
    resolved = raw.resolve()
    once = bool(getattr(args, "once", False))
    if resolved.exists():
        path: Optional[Path] = resolved
        experiment = args.experiment or _default_experiment(resolved)
    else:
        experiment = args.experiment or args.path
        found = connection.execute(
            "SELECT 1 FROM experiments WHERE id=?", (experiment,)
        ).fetchone()
        if found is None:
            raise ValueError(
                "No traces at %s and no imported experiment named %s"
                % (args.path, experiment)
            )
        path = None
        once = True
    output = (
        _path(args.output)
        if args.output
        else _path(args.db).parent / ("%s-report.json" % experiment)
    )
    gold = _path(args.gold) if args.gold else None
    run_pipeline(
        connection,
        path,
        experiment,
        args.model,
        args.max_input_tokens,
        args.force,
        once,
        output,
        gold,
        args.poll_seconds,
        args.lost_after_seconds,
    )


def _add_run_flags(sub: argparse.ArgumentParser, include_once: bool) -> None:
    sub.add_argument(
        "path", help="Trace directory, Harbor job, or imported experiment name"
    )
    sub.add_argument("--experiment")
    sub.add_argument(
        "--model", default=os.getenv("EVALPLANT_JUDGE_MODEL", "deepseek-v4-pro")
    )
    sub.add_argument("--max-input-tokens", type=int, default=DEFAULT_MAX_INPUT_TOKENS)
    sub.add_argument("--force", action="store_true")
    sub.add_argument("--gold", help="Optional gold JSONL for accuracy after the report")
    sub.add_argument("--output", help="Write the JSON report to this path")
    sub.add_argument("--poll-seconds", type=float, default=3.0)
    sub.add_argument("--lost-after-seconds", type=int, default=90)
    if include_once:
        sub.add_argument(
            "--once",
            action="store_true",
            help="Do not watch; process current traces and exit",
        )
    sub.set_defaults(handler=command_run)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="evalplant",
        description="Outcome-first offline evaluation and diagnosis for coding agents",
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--db", default=os.getenv("EVALPLANT_DB", "data/evalplant.db"))
    commands = root.add_subparsers(dest="command", required=True)

    sub = commands.add_parser(
        "run",
        help="Import, diagnose failures, and report; watch live Harbor jobs",
    )
    _add_run_flags(sub, include_once=True)
    sub.set_defaults(once=False)

    sub = commands.add_parser(
        "diagnose", help="One-shot import, diagnose, and report (run --once)"
    )
    _add_run_flags(sub, include_once=False)
    sub.set_defaults(once=True)

    sub = commands.add_parser("import", help="Import ATIF or legacy trajectories")
    sub.add_argument("path")
    sub.add_argument("--experiment", required=True)
    sub.add_argument("--agent-model")
    sub.set_defaults(handler=command_import)

    sub = commands.add_parser(
        "inspect", help="Show one trajectory and its latest diagnosis"
    )
    sub.add_argument("trajectory")
    sub.set_defaults(handler=command_inspect)

    sub = commands.add_parser(
        "observe", help="Import and show Harbor trial lifecycle events"
    )
    sub.add_argument("path", help="Harbor job directory")
    sub.add_argument("--experiment", required=True)
    sub.add_argument("--lost-after-seconds", type=int, default=90)
    sub.add_argument("--output", help="Also write machine-readable JSON status")
    sub.set_defaults(handler=command_observe)

    sub = commands.add_parser("analyze", help="Diagnose failed trajectories")
    sub.add_argument("--experiment", required=True)
    sub.add_argument(
        "--model", default=os.getenv("EVALPLANT_JUDGE_MODEL", "deepseek-v4-pro")
    )
    sub.add_argument("--max-input-tokens", type=int, default=DEFAULT_MAX_INPUT_TOKENS)
    sub.add_argument("--trajectory", help="Diagnose only this trajectory ID")
    sub.add_argument("--force", action="store_true")
    sub.set_defaults(handler=command_analyze)

    sub = commands.add_parser("report", help="Show diagnosis and runtime statistics")
    sub.add_argument("--experiment", required=True)
    sub.add_argument("--output", help="Also write a machine-readable JSON report")
    sub.set_defaults(handler=command_report)

    sub = commands.add_parser("compare", help="Compare two experiments on shared tasks")
    sub.add_argument("--baseline", required=True)
    sub.add_argument("--candidate", required=True)
    sub.add_argument("--k", type=int, default=1)
    sub.add_argument("--max-cost-increase", type=float, default=0.2)
    sub.add_argument("--output", help="Write machine-readable comparison JSON")
    sub.set_defaults(handler=command_compare)
    return root


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    connection = None
    try:
        connection = connect(_path(args.db))
        args.handler(args, connection)
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        error_console.print("[bold red]Error:[/bold red] %s" % error)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
