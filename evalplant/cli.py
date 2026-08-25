import argparse
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .attribution_bench import (
    compare_attribution_runs,
    convert_who_when,
    run_attribution_directory,
)
from .bugsinpy import (
    prepare_task,
    prepare_task_in_docker,
    run_agent,
    run_agent_in_docker,
)
from .companion import evaluate_companion, export_companion_labels, generate_companion
from .core import (
    CATEGORIES,
    PHASES,
    SUBCATEGORIES,
    validate_taxonomy,
)
from .db import (
    claim_attribution_job,
    connect,
    enqueue_attribution,
    export_annotation_template,
    failed_trajectories,
    finish_attribution_job,
    get_steps,
    get_trajectory,
    import_annotations,
    import_run,
    save_annotation,
    save_attribution,
)
from .judge import analyze_trajectory
from .metrics import compare_experiments, report
from .online import serve

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


def command_export_labels(
    args: argparse.Namespace, connection: sqlite3.Connection
) -> None:
    count = export_annotation_template(connection, args.experiment, _path(args.output))
    console.print(
        "Exported [bold green]%s[/bold green] rows to %s" % (count, args.output)
    )


def command_import_labels(
    args: argparse.Namespace, connection: sqlite3.Connection
) -> None:
    count = import_annotations(connection, _path(args.path))
    console.print("Imported [bold green]%s[/bold green] human labels" % count)


def command_attribution_prepare(
    args: argparse.Namespace, connection: sqlite3.Connection
) -> None:
    manifest = convert_who_when(_path(args.source), _path(args.output), args.limit)
    console.print_json(data=manifest)


def command_attribution_run(
    args: argparse.Namespace, connection: sqlite3.Connection
) -> None:
    count = run_attribution_directory(
        _path(args.cases),
        _path(args.output),
        args.method,
        args.model,
        args.limit,
        args.force,
        args.max_chars,
        args.split,
    )
    console.print(
        "Completed [bold green]%s[/bold green] %s attribution case(s)"
        % (count, args.method)
    )


def command_attribution_compare(
    args: argparse.Namespace, connection: sqlite3.Connection
) -> None:
    result = compare_attribution_runs(
        _path(args.raw_results),
        _path(args.graph_results),
        _path(args.labels),
        args.split,
    )
    if args.output:
        output = _path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    console.print_json(data=result)


def command_companion_eval(
    args: argparse.Namespace, connection: sqlite3.Connection
) -> None:
    result = evaluate_companion(
        _path(args.cases),
        _path(args.responses),
        _path(args.output),
        args.model,
    )
    console.print_json(data=result["summary"])


def command_companion_generate(
    args: argparse.Namespace, connection: sqlite3.Connection
) -> None:
    count = generate_companion(_path(args.cases), _path(args.output), args.model)
    console.print("Generated [bold green]%s[/bold green] companion responses" % count)


def command_companion_labels(
    args: argparse.Namespace, connection: sqlite3.Connection
) -> None:
    count = export_companion_labels(_path(args.cases), _path(args.output))
    console.print("Exported [bold green]%s[/bold green] companion cases" % count)


def command_inspect(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    trajectory = get_trajectory(connection, args.trajectory)
    attribution = connection.execute(
        "SELECT * FROM attributions WHERE trajectory_id=?", (args.trajectory,)
    ).fetchone()
    lines = [
        "Task: %s" % trajectory["base_task_id"],
        "Health: %s" % (trajectory["health_status"] or "VALID"),
        "Verdict: %s" % trajectory["verdict"],
        "Reward: %s" % trajectory["reward"],
    ]
    if attribution:
        lines += [
            "First error: %s" % attribution["first_error_step"],
            "Phase: %s" % (attribution["stage"] or "unattributable"),
            "Category: %s" % (attribution["mechanism"] or "unattributable"),
            "Subcategory: %s" % (attribution["subcategory"] or "unattributable"),
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
    stage = _prompt(args.stage, "phase")
    mechanism = _prompt(args.mechanism, "category")
    subcategory = _prompt(args.subcategory, "subcategory")
    validate_taxonomy(stage, mechanism, subcategory)
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
        subcategory,
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
            console.print("[dim]%s already analyzed[/dim]" % trajectory["base_task_id"])
            continue
        result = analyze_trajectory(
            Path(trajectory["raw_path"]),
            _read_optional(trajectory["final_patch_path"]),
            _read_optional(trajectory["final_log_path"]),
            args.model,
        )
        save_attribution(connection, trajectory["id"], result)
        console.print(
            "[green]%s[/green] step=%s phase=%s category=%s subcategory=%s"
            % (
                trajectory["base_task_id"],
                result.get("first_error_step"),
                result.get("stage"),
                result.get("mechanism"),
                result.get("subcategory"),
            )
        )


def command_enqueue(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    count = sum(
        enqueue_attribution(connection, row["id"])
        for row in failed_trajectories(connection, args.experiment)
    )
    console.print("Queued [bold green]%s[/bold green] failed trajectories" % count)


def command_worker(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    while True:
        trajectory = claim_attribution_job(connection)
        if trajectory is None:
            if args.once:
                console.print("No pending attribution jobs")
                return
            time.sleep(args.poll_seconds)
            continue
        try:
            result = analyze_trajectory(
                Path(trajectory["raw_path"]),
                _read_optional(trajectory["final_patch_path"]),
                _read_optional(trajectory["final_log_path"]),
                args.model,
            )
            save_attribution(connection, trajectory["id"], result)
            finish_attribution_job(connection, trajectory["id"])
            console.print("[green]Attributed %s[/green]" % trajectory["base_task_id"])
        except Exception as error:
            finish_attribution_job(connection, trajectory["id"], str(error))
            error_console.print("[red]Attribution failed:[/red] %s" % error)
        if args.once:
            return


def command_serve(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    connection.close()
    console.print("Listening on http://%s:%s/ingest" % (args.host, args.port))
    try:
        serve(_path(args.db), _path(args.store), args.host, args.port)
    except KeyboardInterrupt:
        console.print("Stopped")


def _percent(value: Optional[float]) -> str:
    return "n/a" if value is None else "%.1f%%" % (value * 100)


def _number_text(value: Optional[float], suffix: str = "") -> str:
    return "n/a" if value is None else "%s%s" % (f"{value:,.1f}", suffix)


def command_report(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    result = report(connection, args.experiment, args.split)
    table = Table("Metric", "Value")
    table.add_row("Verdicts", json.dumps(result["verdicts"], sort_keys=True))
    table.add_row("Average reward", _percent(result["average_reward"]))
    table.add_row("Pass all repeats", _percent(result["pass_all_repeats"]))
    table.add_row("Pass@3", _percent(result["pass_at_3"]))
    table.add_row(
        "Valid trials / unique tasks",
        "%s / %s" % (result["valid_trials"], result["unique_tasks"]),
    )
    table.add_row("Tasks with 3+ repeats", str(result["repeated_tasks"]))
    table.add_row("Unstable repeated tasks", _percent(result["unstable_task_rate"]))
    table.add_row("Average steps", _number_text(result["average_steps"]))
    table.add_row("Average tool errors", _number_text(result["average_tool_errors"]))
    table.add_row(
        "Average input / cache tokens",
        "%s / %s"
        % (
            _number_text(result["average_input_tokens"]),
            _number_text(result["average_cache_tokens"]),
        ),
    )
    table.add_row(
        "Average output tokens", _number_text(result["average_output_tokens"])
    )
    table.add_row(
        "Environment / agent setup",
        "%s / %s"
        % (
            _number_text(result["average_environment_setup_seconds"], "s"),
            _number_text(result["average_agent_setup_seconds"], "s"),
        ),
    )
    table.add_row(
        "Agent / verifier execution",
        "%s / %s"
        % (
            _number_text(result["average_agent_execution_seconds"], "s"),
            _number_text(result["average_verifier_seconds"], "s"),
        ),
    )
    table.add_row(
        "Annotations / evaluated",
        "%s / %s" % (result["annotations"], result["evaluated"]),
    )
    for key in (
        "exact_step_accuracy",
        "near_step_accuracy",
        "stage_macro_f1",
        "mechanism_macro_f1",
        "subcategory_macro_f1",
        "evidence_pass_rate",
        "attribution_coverage",
    ):
        table.add_row(key, _percent(result[key]))
    console.print(Panel(table, title="%s · %s" % (args.experiment, args.split)))
    if result["security_metrics"]:
        security = Table("Security metric", "Rate", "Samples")
        for name, metric in result["security_metrics"].items():
            security.add_row(name, _percent(metric["mean"]), str(metric["samples"]))
        console.print(Panel(security, title="Red-team metrics"))


def command_compare(args: argparse.Namespace, connection: sqlite3.Connection) -> None:
    result = compare_experiments(connection, args.experiment_a, args.experiment_b)
    table = Table(
        "Task", "Reward A/B", "Input tokens A/B", "Agent sec A/B", "Steps A/B"
    )
    for row in result["tasks"]:
        table.add_row(
            row["task_id"],
            "%s / %s" % (_number_text(row["reward_a"]), _number_text(row["reward_b"])),
            "%s / %s"
            % (
                _number_text(row["input_tokens_a"]),
                _number_text(row["input_tokens_b"]),
            ),
            "%s / %s"
            % (
                _number_text(row["agent_seconds_a"]),
                _number_text(row["agent_seconds_b"]),
            ),
            "%s / %s" % (_number_text(row["steps_a"]), _number_text(row["steps_b"])),
        )
    console.print(
        Panel(
            table,
            title="%s (A) vs %s (B)" % (args.experiment_a, args.experiment_b),
        )
    )
    console.print(
        "Average reward: %s vs %s"
        % (
            _percent(result["summary_a"]["average_reward"]),
            _percent(result["summary_b"]["average_reward"]),
        )
    )
    if result["only_a"] or result["only_b"]:
        console.print(
            "Unpaired tasks: A=%s B=%s" % (result["only_a"], result["only_b"])
        )


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


def command_docker_prepare(
    args: argparse.Namespace, connection: sqlite3.Connection
) -> None:
    metadata = prepare_task_in_docker(
        _path(args.bugsinpy_root),
        args.project,
        args.bug,
        _path(args.workspace),
        _path(args.oracle_dir),
        args.image,
        args.timeout,
        args.platform,
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


def command_docker_run(
    args: argparse.Namespace, connection: sqlite3.Connection
) -> None:
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
    console.print(
        "Container run saved and imported: [bold green]%s[/bold green]" % ids[0]
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="evalplant", description="Harbor coding-agent evaluation and attribution"
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--db", default=os.getenv("EVALPLANT_DB", "data/evalplant.db"))
    commands = root.add_subparsers(dest="command", required=True)

    sub = commands.add_parser(
        "import", help="Import Harbor ATIF or legacy trajectories"
    )
    sub.add_argument("path")
    sub.add_argument("--experiment", required=True)
    sub.add_argument("--agent-model")
    sub.set_defaults(handler=command_import)

    sub = commands.add_parser(
        "export-labels", help="Export failed trajectories for human review"
    )
    sub.add_argument("--experiment", required=True)
    sub.add_argument("--output", required=True)
    sub.set_defaults(handler=command_export_labels)

    sub = commands.add_parser("import-labels", help="Import completed human labels")
    sub.add_argument("path")
    sub.set_defaults(handler=command_import_labels)

    sub = commands.add_parser(
        "attribution-prepare", help="Convert Who&When while isolating gold labels"
    )
    sub.add_argument("source")
    sub.add_argument("--output", required=True)
    sub.add_argument("--limit", type=int, default=0)
    sub.set_defaults(handler=command_attribution_prepare)

    sub = commands.add_parser(
        "attribution-run", help="Run one fair two-pass attribution method"
    )
    sub.add_argument("cases")
    sub.add_argument("--output", required=True)
    sub.add_argument("--method", choices=("raw", "graph"), required=True)
    sub.add_argument(
        "--model", default=os.getenv("EVALPLANT_JUDGE_MODEL", "deepseek-v4-pro")
    )
    sub.add_argument("--limit", type=int, default=0)
    sub.add_argument("--max-chars", type=int, default=24000)
    sub.add_argument("--split", choices=("dev", "test"))
    sub.add_argument("--force", action="store_true")
    sub.set_defaults(handler=command_attribution_run)

    sub = commands.add_parser(
        "attribution-compare", help="Compare Raw and Graph results against hidden gold"
    )
    sub.add_argument("--raw-results", required=True)
    sub.add_argument("--graph-results", required=True)
    sub.add_argument("--labels", required=True)
    sub.add_argument("--split", choices=("dev", "test"))
    sub.add_argument("--output")
    sub.set_defaults(handler=command_attribution_compare)

    sub = commands.add_parser(
        "companion-eval", help="Score social-companion responses with a Judge"
    )
    sub.add_argument("--cases", default="benchmarks/companion/cases.jsonl")
    sub.add_argument("--responses", required=True)
    sub.add_argument("--output", required=True)
    sub.add_argument(
        "--model", default=os.getenv("EVALPLANT_JUDGE_MODEL", "deepseek-v4-pro")
    )
    sub.set_defaults(handler=command_companion_eval)

    sub = commands.add_parser(
        "companion-generate", help="Generate responses for companion cases"
    )
    sub.add_argument("--cases", default="benchmarks/companion/cases.jsonl")
    sub.add_argument("--output", required=True)
    sub.add_argument(
        "--model", default=os.getenv("EVALPLANT_AGENT_MODEL", "deepseek-v4-flash")
    )
    sub.set_defaults(handler=command_companion_generate)

    sub = commands.add_parser(
        "companion-labels", help="Export a blank human calibration sheet"
    )
    sub.add_argument("--cases", default="benchmarks/companion/cases.jsonl")
    sub.add_argument("--output", required=True)
    sub.set_defaults(handler=command_companion_labels)

    sub = commands.add_parser("inspect", help="Show one normalized trajectory")
    sub.add_argument("trajectory")
    sub.set_defaults(handler=command_inspect)

    sub = commands.add_parser("annotate", help="Save a human attribution label")
    sub.add_argument("trajectory")
    sub.add_argument("--split", choices=("dev", "test"))
    sub.add_argument("--step")
    sub.add_argument("--phase", "--stage", dest="stage", choices=PHASES)
    sub.add_argument("--category", "--mechanism", dest="mechanism", choices=CATEGORIES)
    sub.add_argument("--subcategory", choices=SUBCATEGORIES)
    sub.add_argument("--evidence")
    sub.add_argument("--evidence-pass", choices=("yes", "no"))
    sub.add_argument("--notes")
    sub.add_argument("--oracle-used", action="store_true")
    sub.set_defaults(handler=command_annotate)

    sub = commands.add_parser(
        "analyze", help="Run one evidence-grounded DeepSeek Judge"
    )
    sub.add_argument("--experiment", required=True)
    sub.add_argument(
        "--model", default=os.getenv("EVALPLANT_JUDGE_MODEL", "deepseek-v4-pro")
    )
    sub.add_argument("--force", action="store_true")
    sub.set_defaults(handler=command_analyze)

    sub = commands.add_parser("enqueue", help="Queue valid failed trajectories")
    sub.add_argument("--experiment", required=True)
    sub.set_defaults(handler=command_enqueue)

    sub = commands.add_parser("worker", help="Process the SQLite attribution queue")
    sub.add_argument(
        "--model", default=os.getenv("EVALPLANT_JUDGE_MODEL", "deepseek-v4-pro")
    )
    sub.add_argument("--once", action="store_true")
    sub.add_argument("--poll-seconds", type=float, default=2.0)
    sub.set_defaults(handler=command_worker)

    sub = commands.add_parser("serve", help="Receive completed online ATIF traces")
    sub.add_argument("--host", default="127.0.0.1")
    sub.add_argument("--port", type=int, default=8787)
    sub.add_argument("--store", default="data/online")
    sub.set_defaults(handler=command_serve)

    sub = commands.add_parser("report", help="Report attribution metrics")
    sub.add_argument("--experiment", required=True)
    sub.add_argument("--split", choices=("dev", "test"), default="test")
    sub.set_defaults(handler=command_report)

    sub = commands.add_parser("compare", help="Compare two experiments task by task")
    sub.add_argument("--experiment-a", required=True)
    sub.add_argument("--experiment-b", required=True)
    sub.set_defaults(handler=command_compare)

    sub = commands.add_parser("prepare", help="Prepare and sanitize one BugsInPy task")
    sub.add_argument("--bugsinpy-root", required=True)
    sub.add_argument("--project", required=True)
    sub.add_argument("--bug", required=True, type=int)
    sub.add_argument("--workspace", required=True)
    sub.add_argument("--oracle-dir", default="data/oracle")
    sub.add_argument("--timeout", type=int, default=1800)
    sub.set_defaults(handler=command_prepare)

    sub = commands.add_parser(
        "docker-prepare", help="Prepare one task at the fixed container path /task"
    )
    sub.add_argument("--bugsinpy-root", required=True)
    sub.add_argument("--project", required=True)
    sub.add_argument("--bug", required=True, type=int)
    sub.add_argument("--workspace", required=True)
    sub.add_argument("--oracle-dir", default="data/oracle")
    sub.add_argument("--image", default="evalplant-agent:0.2")
    sub.add_argument("--platform", default="linux/amd64")
    sub.add_argument("--timeout", type=int, default=1800)
    sub.set_defaults(handler=command_docker_prepare)

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
