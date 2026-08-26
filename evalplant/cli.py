import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .db import (
    connect,
    diagnosable_trajectories,
    get_diagnosis,
    get_steps,
    get_trajectory,
    import_run,
    save_diagnosis,
)
from .judge import DEFAULT_MAX_INPUT_TOKENS, analyze_trajectory, failed_diagnosis
from .metrics import report

console = Console()
error_console = Console(stderr=True)


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _read_optional(value: Optional[str]) -> str:
    return _path(value).read_text(encoding="utf-8", errors="replace") if value else ""


def command_import(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    ids = import_run(connection, _path(args.path), args.experiment, args.agent_model)
    console.print("Imported [bold green]%s[/bold green] trajectory(s): %s" % (len(ids), ", ".join(ids)))


def command_inspect(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    trajectory = get_trajectory(connection, args.trajectory)
    diagnosis = get_diagnosis(connection, args.trajectory)
    lines = [
        "Task: %s" % trajectory["base_task_id"],
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
            "Category: %s %s" % (diagnosis["category_code"] or "", diagnosis["category_name"] or ""),
            "Component: %s" % (diagnosis["component"] or "n/a"),
            "Confidence: %s" % (diagnosis["confidence"] or "n/a"),
            "Summary: %s" % diagnosis["summary"],
        ]
        root_step = diagnosis["root_cause_step"]
        detail = json.loads(diagnosis["report_json"])
        evidence_steps = {
            item["step_id"] for item in detail.get("evidence", []) if item.get("step_id") is not None
        }
    console.print(Panel("\n".join(lines), title="Trajectory %s" % trajectory["id"]))

    table = Table("Step", "Role", "Type", "Command / content", "Test")
    for step in get_steps(connection, args.trajectory):
        marker = "★" if step["step_index"] == root_step else ("•" if step["step_index"] in evidence_steps else "")
        text = step["command"] or step["content_preview"].replace("\n", " ")
        table.add_row(
            "%s %s" % (step["step_index"], marker),
            step["role"],
            step["action_type"],
            Text(text[:120]),
            step["test_status"] or "",
        )
    console.print(table)


def command_analyze(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    trajectories = diagnosable_trajectories(connection, args.experiment)
    if args.trajectory:
        trajectories = [row for row in trajectories if row["id"] == args.trajectory]
    if not trajectories:
        raise ValueError("No diagnosable trajectories in experiment %s" % args.experiment)
    connection.execute("UPDATE experiments SET judge_model=? WHERE id=?", (args.model, args.experiment))
    connection.commit()
    for trajectory in trajectories:
        if get_diagnosis(connection, trajectory["id"]) and not args.force:
            console.print("[dim]%s already diagnosed[/dim]" % trajectory["base_task_id"])
            continue
        try:
            result = analyze_trajectory(
                Path(trajectory["raw_path"]),
                trajectory["verdict"],
                trajectory["health_status"],
                _read_optional(trajectory["final_log_path"]),
                args.model,
                args.max_input_tokens,
            )
        except Exception as error:
            result = failed_diagnosis(error, args.model)
            result["max_input_tokens"] = args.max_input_tokens
        save_diagnosis(connection, trajectory["id"], result)
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
            error_console.print("[red]Diagnosis failed:[/red] %s" % result["diagnosis_error"])


def _number(value: Optional[float], suffix: str = "") -> str:
    return "n/a" if value is None else "%s%s" % (f"{value:,.2f}", suffix)


def command_report(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    result = report(connection, args.experiment)
    if args.output:
        diagnoses = connection.execute(
            """
            SELECT t.id, t.base_task_id, t.verdict, t.health_status, d.report_json
            FROM trajectories t JOIN diagnoses d ON d.trajectory_id=t.id
            WHERE t.experiment_id=? ORDER BY t.base_task_id, t.trial_name
            """,
            (args.experiment,),
        ).fetchall()
        payload = {
            "statistics": result,
            "diagnoses": [
                {
                    "trajectory_id": row["id"],
                    "task_id": row["base_task_id"],
                    "verdict": row["verdict"],
                    "health_status": row["health_status"],
                    "diagnosis": json.loads(row["report_json"]),
                }
                for row in diagnoses
            ],
        }
        output = _path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        console.print("Exported report to [bold green]%s[/bold green]" % output)
    table = Table("Metric", "Value")
    for label, value in (
        ("Total tasks", result["total_tasks"]),
        ("Successful tasks", result["successful_tasks"]),
        ("Failed tasks", result["failed_tasks"]),
        ("Verdicts", json.dumps(result["verdicts"], ensure_ascii=False, sort_keys=True)),
        ("Diagnosis statuses", json.dumps(result["diagnosis_statuses"], ensure_ascii=False, sort_keys=True)),
        ("Responsibilities", json.dumps(result["responsibilities"], ensure_ascii=False, sort_keys=True)),
        ("Harness layers", json.dumps(result["harness_layers"], ensure_ascii=False, sort_keys=True)),
        ("LLM categories", json.dumps(result["llm_categories"], ensure_ascii=False, sort_keys=True)),
        ("Confidence", json.dumps(result["confidence"], ensure_ascii=False, sort_keys=True)),
        ("Decision source", json.dumps(result["decision_sources"], ensure_ascii=False, sort_keys=True)),
        ("Components", json.dumps(result["components"], ensure_ascii=False, sort_keys=True)),
        ("Average input tokens", _number(result["average_input_tokens"])),
        ("Average cache tokens", _number(result["average_cache_tokens"])),
        ("Average output tokens", _number(result["average_output_tokens"])),
        ("Average run cost", _number(result["average_cost"], " USD")),
        ("Average agent time", _number(result["average_agent_seconds"], "s")),
        ("Average verifier time", _number(result["average_verifier_seconds"], "s")),
        ("Diagnosis input/output tokens", "%s / %s" % (result["diagnosis_input_tokens"], result["diagnosis_output_tokens"])),
        ("Diagnosis latency", _number(result["diagnosis_latency_seconds"], "s")),
    ):
        table.add_row(str(label), str(value))
    console.print(Panel(table, title=args.experiment))

    grouped = Table("Group", "Total", "Pass", "Fail", "Harness", "LLM")
    for prefix, rows in (("model", result["by_model"]), ("agent", result["by_agent_version"])):
        for name, values in rows.items():
            grouped.add_row(
                "%s:%s" % (prefix, name),
                str(values["total"]), str(values["passed"]), str(values["failed"]),
                str(values["harness"]), str(values["llm"]),
            )
    console.print(Panel(grouped, title="Breakdown"))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="evalplant", description="Harbor trajectory diagnosis and statistics")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--db", default=os.getenv("EVALPLANT_DB", "data/evalplant.db"))
    commands = root.add_subparsers(dest="command", required=True)

    sub = commands.add_parser("import", help="Import Harbor ATIF trajectories")
    sub.add_argument("path")
    sub.add_argument("--experiment", required=True)
    sub.add_argument("--agent-model")
    sub.set_defaults(handler=command_import)

    sub = commands.add_parser("inspect", help="Show one trajectory and its latest diagnosis")
    sub.add_argument("trajectory")
    sub.set_defaults(handler=command_inspect)

    sub = commands.add_parser("analyze", help="Diagnose failed Harbor trajectories")
    sub.add_argument("--experiment", required=True)
    sub.add_argument("--model", default=os.getenv("EVALPLANT_JUDGE_MODEL", "deepseek-v4-pro"))
    sub.add_argument("--max-input-tokens", type=int, default=DEFAULT_MAX_INPUT_TOKENS)
    sub.add_argument("--trajectory", help="Diagnose only this trajectory ID")
    sub.add_argument("--force", action="store_true")
    sub.set_defaults(handler=command_analyze)

    sub = commands.add_parser("report", help="Show diagnosis and runtime statistics")
    sub.add_argument("--experiment", required=True)
    sub.add_argument("--output", help="Also write a machine-readable JSON report")
    sub.set_defaults(handler=command_report)
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
