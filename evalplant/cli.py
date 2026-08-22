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
from .bugsinpy import prepare_task, run_agent, run_agent_in_docker
from .core import MECHANISMS, STAGES, validate_stage_mechanism
from .db import (
    connect,
    failed_trajectories,
    get_steps,
    get_trajectory,
    import_run,
    save_annotation,
    save_attribution,
)
from .judge import analyze_trajectory
from .metrics import report

console = Console()
error_console = Console(stderr=True)


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _read_optional(path: Optional[str]) -> str:
    return _path(path).read_text(encoding="utf-8", errors="replace") if path else ""


def command_import(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    ids = import_run(connection, _path(args.path), args.experiment, args.agent_model)
    console.print(
        "Imported [bold green]%s[/bold green] trajectory(s): %s"
        % (len(ids), ", ".join(ids))
    )


def command_inspect(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    trajectory = get_trajectory(connection, args.trajectory)
    attribution = connection.execute(
        "SELECT * FROM attributions WHERE trajectory_id=?", (args.trajectory,)
    ).fetchone()
    lines = [
        "Task: %s" % trajectory["task_id"],
        "Verdict: %s" % trajectory["verdict"],
    ]
    if attribution:
        lines += [
            "First error: %s" % attribution["first_error_step"],
            "Stage: %s" % (attribution["stage"] or "unattributable"),
            "Mechanism: %s" % (attribution["mechanism"] or "unattributable"),
            "Confidence: %s"
            % (
                "%.0f%%" % (attribution["confidence"] * 100)
                if attribution["confidence"] is not None
                else "n/a"
            ),
            "Summary: %s" % attribution["summary"],
        ]
    console.print(Panel("\n".join(lines), title="Trajectory %s" % trajectory["id"]))

    error_step = attribution["first_error_step"] if attribution else None
    evidence = (
        set(json.loads(attribution["evidence_step_ids"])) if attribution else set()
    )
    table = Table("Step", "Type", "Command / content", "Test")
    for step in get_steps(connection, args.trajectory):
        marker = (
            "★"
            if step["step_index"] == error_step
            else ("•" if step["step_index"] in evidence else "")
        )
        text = step["command"] or step["content_preview"].replace("\n", " ")
        table.add_row(
            "%s %s" % (step["step_index"], marker),
            step["action_type"],
            Text(text[:120]),
            step["test_status"] or "",
        )
    console.print(table)


def _prompt(value: Optional[str], label: str) -> str:
    return value if value is not None else input("%s: " % label).strip()


def command_annotate(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    get_trajectory(connection, args.trajectory)
    valid_steps = {row["step_index"] for row in get_steps(connection, args.trajectory)}
    split = _prompt(args.split, "split (dev/test)")
    if split not in ("dev", "test"):
        raise ValueError("split must be dev or test")
    first_error_step = int(_prompt(args.step, "first error step"))
    if first_error_step not in valid_steps:
        raise ValueError("Unknown step: %s" % first_error_step)
    stage = _prompt(args.stage, "stage")
    mechanism = _prompt(args.mechanism, "mechanism")
    validate_stage_mechanism(stage, mechanism)
    evidence_text = _prompt(args.evidence, "evidence steps, comma-separated")
    evidence = [int(item.strip()) for item in evidence_text.split(",") if item.strip()]
    if any(item not in valid_steps for item in evidence):
        raise ValueError("Evidence references an unknown step")
    evidence_pass = None
    if args.evidence_pass is not None:
        evidence_pass = args.evidence_pass == "yes"
    save_annotation(
        connection,
        args.trajectory,
        split,
        first_error_step,
        stage,
        mechanism,
        evidence,
        evidence_pass,
        args.notes or "",
        args.oracle_used,
    )
    console.print("Saved annotation for [bold green]%s[/bold green]" % args.trajectory)


def command_analyze(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    trajectories = failed_trajectories(connection, args.experiment)
    if not trajectories:
        raise ValueError(
            "No FAIL or TIMEOUT trajectories in experiment %s" % args.experiment
        )
    connection.execute(
        "UPDATE experiments SET judge_model=? WHERE id=?",
        (args.model, args.experiment),
    )
    connection.commit()
    for trajectory in trajectories:
        cached = connection.execute(
            "SELECT 1 FROM attributions WHERE trajectory_id=?", (trajectory["id"],)
        ).fetchone()
        if cached and not args.force:
            console.print("[dim]%s already analyzed[/dim]" % trajectory["task_id"])
            continue
        final_patch = _read_optional(trajectory["final_patch_path"])
        final_log = _read_optional(trajectory["final_log_path"])
        result = analyze_trajectory(
            Path(trajectory["raw_path"]), final_patch, final_log, args.model
        )
        save_attribution(connection, trajectory["id"], result)
        console.print(
            "[green]%s[/green] step=%s stage=%s mechanism=%s"
            % (
                trajectory["task_id"],
                result.get("first_error_step"),
                result.get("stage"),
                result.get("mechanism"),
            )
        )


def _percent(value: Optional[float]) -> str:
    return "n/a" if value is None else "%.1f%%" % (value * 100)


def command_report(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    result = report(connection, args.experiment, args.split)
    table = Table("Metric", "Value")
    table.add_row("Verdicts", json.dumps(result["verdicts"], sort_keys=True))
    table.add_row(
        "Annotations / evaluated",
        "%s / %s" % (result["annotations"], result["evaluated"]),
    )
    for key in (
        "exact_step_accuracy",
        "near_step_accuracy",
        "stage_macro_f1",
        "mechanism_macro_f1",
        "evidence_pass_rate",
        "attribution_coverage",
    ):
        table.add_row(key, _percent(result[key]))
    console.print(Panel(table, title="%s · %s" % (args.experiment, args.split)))


def command_prepare(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    metadata = prepare_task(
        _path(args.bugsinpy_root),
        args.project,
        args.bug,
        _path(args.workspace),
        _path(args.oracle_dir),
        args.timeout,
    )
    console.print_json(data=metadata)


def command_run(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    task_dir = run_agent(
        _path(args.workspace),
        _path(args.run_dir),
        args.model,
        args.timeout,
        args.allow_host_execution,
        args.step_limit,
    )
    ids = import_run(connection, task_dir, args.experiment, args.model)
    console.print("Run saved and imported: [bold green]%s[/bold green]" % ids[0])


def command_execute(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    task_dir = run_agent(
        _path(args.workspace),
        _path(args.run_dir),
        args.model,
        args.timeout,
        True,
        args.step_limit,
    )
    console.print(str(task_dir))


def command_docker_run(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    task_dir = run_agent_in_docker(
        _path(args.workspace),
        _path(args.run_dir),
        args.image,
        args.model,
        args.timeout,
        args.step_limit,
        args.platform,
    )
    ids = import_run(connection, task_dir, args.experiment, args.model)
    console.print("Container run saved and imported: [bold green]%s[/bold green]" % ids[0])


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="evalplant", description="Offline coding-agent failure attribution"
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--db", default=os.getenv("EVALPLANT_DB", "data/evalplant.db"))
    commands = root.add_subparsers(dest="command", required=True)

    sub = commands.add_parser("import", help="Import mini-SWE-agent trajectories")
    sub.add_argument("path")
    sub.add_argument("--experiment", required=True)
    sub.add_argument("--agent-model")
    sub.set_defaults(handler=command_import)

    sub = commands.add_parser("inspect", help="Show one normalized trajectory")
    sub.add_argument("trajectory")
    sub.set_defaults(handler=command_inspect)

    sub = commands.add_parser("annotate", help="Save a human attribution label")
    sub.add_argument("trajectory")
    sub.add_argument("--split", choices=("dev", "test"))
    sub.add_argument("--step")
    sub.add_argument("--stage", choices=STAGES)
    sub.add_argument("--mechanism", choices=MECHANISMS)
    sub.add_argument("--evidence")
    sub.add_argument("--evidence-pass", choices=("yes", "no"))
    sub.add_argument("--notes")
    sub.add_argument("--oracle-used", action="store_true")
    sub.set_defaults(handler=command_annotate)

    sub = commands.add_parser("analyze", help="Run two-stage DeepSeek attribution")
    sub.add_argument("--experiment", required=True)
    sub.add_argument(
        "--model", default=os.getenv("EVALPLANT_JUDGE_MODEL", "deepseek-v4-pro")
    )
    sub.add_argument("--force", action="store_true")
    sub.set_defaults(handler=command_analyze)

    sub = commands.add_parser("report", help="Report attribution metrics")
    sub.add_argument("--experiment", required=True)
    sub.add_argument("--split", choices=("dev", "test"), default="test")
    sub.set_defaults(handler=command_report)

    sub = commands.add_parser("prepare", help="Prepare and sanitize one BugsInPy task")
    sub.add_argument("--bugsinpy-root", required=True)
    sub.add_argument("--project", required=True)
    sub.add_argument("--bug", required=True, type=int)
    sub.add_argument("--workspace", required=True)
    sub.add_argument("--oracle-dir", default="data/oracle")
    sub.add_argument("--timeout", type=int, default=1800)
    sub.set_defaults(handler=command_prepare)

    sub = commands.add_parser("run", help="Run mini-SWE-agent in a prepared workspace")
    sub.add_argument("workspace")
    sub.add_argument("--experiment", required=True)
    sub.add_argument("--run-dir", default="data/raw")
    sub.add_argument(
        "--model",
        default=os.getenv("EVALPLANT_AGENT_MODEL", "deepseek/deepseek-v4-flash"),
    )
    sub.add_argument("--timeout", type=int, default=1800)
    sub.add_argument("--step-limit", type=int, default=0)
    sub.add_argument("--allow-host-execution", action="store_true")
    sub.set_defaults(handler=command_run)

    sub = commands.add_parser(
        "execute", help="Execute one task inside the isolated agent container"
    )
    sub.add_argument("workspace")
    sub.add_argument("--run-dir", default="/output")
    sub.add_argument(
        "--model",
        default=os.getenv("EVALPLANT_AGENT_MODEL", "deepseek/deepseek-v4-flash"),
    )
    sub.add_argument("--timeout", type=int, default=1800)
    sub.add_argument("--step-limit", type=int, default=0)
    sub.set_defaults(handler=command_execute)

    sub = commands.add_parser(
        "docker-run", help="Run one task in Docker, then import its local artifacts"
    )
    sub.add_argument("workspace")
    sub.add_argument("--experiment", required=True)
    sub.add_argument("--run-dir", default="data/raw")
    sub.add_argument("--image", default="evalplant-agent:0.2")
    sub.add_argument("--platform", default="linux/amd64")
    sub.add_argument(
        "--model",
        default=os.getenv("EVALPLANT_AGENT_MODEL", "deepseek/deepseek-v4-flash"),
    )
    sub.add_argument("--timeout", type=int, default=1800)
    sub.add_argument("--step-limit", type=int, default=0)
    sub.set_defaults(handler=command_docker_run)
    return root


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        connection = connect(_path(args.db))
        args.handler(args, connection)
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        error_console.print("[bold red]Error:[/bold red] %s" % error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
