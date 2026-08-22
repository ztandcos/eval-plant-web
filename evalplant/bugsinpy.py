import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

TASK_TEMPLATE = """The repository contains a bug.

Fix the implementation so that the validation command passes.
Do not modify tests. Inspect the repository, reproduce the failure, implement
a minimal fix, and verify the result.

Validation command:
./evalplant_test.sh
"""


def _run(
    command: List[str],
    cwd: Optional[Path] = None,
    timeout: int = 1800,
    env: Optional[Dict[str, str]] = None,
    stdin: Any = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        env=env,
        stdin=stdin,
    )


def _tool(root: Path, name: str) -> str:
    path = root / "framework" / "bin" / name
    if not path.exists():
        raise ValueError("Missing BugsInPy tool: %s" % path)
    return str(path)


def _task_python(workspace: Path) -> str:
    info = (workspace / "bugsinpy_bug.info").read_text(encoding="utf-8")
    match = re.search(r'python_version="(\d+\.\d+)', info)
    if not match:
        raise ValueError("BugsInPy task does not declare python_version")
    request = match.group(1)
    uv = shutil.which("uv") or "uv"
    uv_env = os.environ.copy()
    uv_env["UV_PYTHON_INSTALL_DIR"] = str(workspace / ".evalplant-python")
    found = _run([uv, "python", "find", "--no-project", request], env=uv_env)
    if found.returncode:
        installed = _run([uv, "python", "install", request], env=uv_env)
        if installed.returncode:
            raise RuntimeError(
                "Could not install Python %s:\n%s" % (request, installed.stdout)
            )
        found = _run([uv, "python", "find", "--no-project", request], env=uv_env)
    if found.returncode:
        raise RuntimeError("Could not find Python %s:\n%s" % (request, found.stdout))
    return found.stdout.strip().splitlines()[-1]


def _requirements_text(path: Path) -> str:
    raw = path.read_bytes()
    if not raw:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw[:100]:
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def _compile(workspace: Path) -> None:
    python = _task_python(workspace)
    result = _run([python, "-m", "venv", "env"], cwd=workspace)
    if result.returncode:
        raise RuntimeError("Could not create task environment:\n%s" % result.stdout)
    task_python = str(workspace / "env" / "bin" / "python")
    task_env = os.environ.copy()
    task_env["PATH"] = str(workspace / "env" / "bin") + os.pathsep + task_env["PATH"]

    requirements = _requirements_text(workspace / "bugsinpy_requirements.txt")
    if requirements.strip():
        normalized = workspace / ".evalplant-requirements.txt"
        normalized.write_text(requirements, encoding="utf-8")
        result = _run(
            [
                task_python,
                "-m",
                "pip",
                "install",
                "--use-deprecated=legacy-resolver",
                "-r",
                str(normalized),
            ],
            cwd=workspace,
            env=task_env,
        )
        normalized.unlink()
        if result.returncode:
            raise RuntimeError("Task requirements failed:\n%s" % result.stdout)

    setup = workspace / "bugsinpy_setup.sh"
    if setup.exists() and setup.read_text(encoding="utf-8").strip():
        result = _run(["bash", str(setup)], cwd=workspace, env=task_env)
        if result.returncode:
            raise RuntimeError("Task setup failed:\n%s" % result.stdout)

    test_requirements = workspace / "test_requirements.txt"
    if test_requirements.exists():
        result = _run(
            [
                task_python,
                "-m",
                "pip",
                "install",
                "--use-deprecated=legacy-resolver",
                "-r",
                str(test_requirements),
            ],
            cwd=workspace,
            env=task_env,
        )
        if result.returncode:
            raise RuntimeError("Test requirements failed:\n%s" % result.stdout)

    result = _run(
        [task_python, "-m", "pip", "install", "pytest<8.4"],
        cwd=workspace,
        env=task_env,
    )
    if result.returncode:
        raise RuntimeError("Could not install pytest:\n%s" % result.stdout)
    (workspace / "bugsinpy_compile_flag").write_text("1\n", encoding="utf-8")


def _checkout(
    root: Path, project: str, bug: int, version: int, workspace: Path
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="evalplant-checkout-", dir=str(workspace.parent)
    ) as checkout_dir:
        result = _run(
            [
                _tool(root, "bugsinpy-checkout"),
                "-p",
                project,
                "-v",
                str(version),
                "-i",
                str(bug),
                "-w",
                checkout_dir,
            ]
        )
        source = Path(checkout_dir) / project
        if result.returncode or not source.exists():
            raise RuntimeError("BugsInPy checkout failed:\n%s" % result.stdout)
        if workspace.exists():
            workspace.rmdir()
        shutil.move(str(source), str(workspace))
    _compile(workspace)


def _validation_script(workspace: Path) -> str:
    source = workspace / "bugsinpy_run_test.sh"
    if not source.exists():
        raise ValueError("Checkout did not create bugsinpy_run_test.sh")
    return """#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ -f env/bin/activate ]; then source env/bin/activate; fi
%s
""" % source.read_text(encoding="utf-8")


def _is_test_path(path: str) -> bool:
    parts = [part.lower() for part in Path(path).parts]
    name = parts[-1] if parts else ""
    return (
        "test" in parts
        or "tests" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _validate(
    workspace: Path, timeout: int, trusted_script: Optional[str] = None
) -> subprocess.CompletedProcess:
    command = (
        ["bash", "-c", trusted_script]
        if trusted_script is not None
        else ["bash", "evalplant_test.sh"]
    )
    return _run(command, cwd=workspace, timeout=timeout)


def prepare_task(
    bugsinpy_root: Path,
    project: str,
    bug: int,
    workspace: Path,
    oracle_dir: Path,
    timeout: int = 1800,
) -> Dict[str, Any]:
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError("Workspace must be empty: %s" % workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    oracle_task_dir = oracle_dir / ("%s-%s" % (project, bug))
    oracle_task_dir.mkdir(parents=True, exist_ok=True)

    _checkout(bugsinpy_root, project, bug, 0, workspace)
    script = _validation_script(workspace)
    (workspace / "evalplant_test.sh").write_text(script, encoding="utf-8")
    os.chmod(str(workspace / "evalplant_test.sh"), 0o755)
    baseline = _validate(workspace, timeout)
    (oracle_task_dir / "baseline_test.log").write_text(
        baseline.stdout, encoding="utf-8"
    )
    if baseline.returncode == 0:
        raise ValueError("Buggy version unexpectedly passes its validation test")

    with tempfile.TemporaryDirectory(prefix="evalplant-fixed-") as temp_dir:
        fixed_workspace = Path(temp_dir) / "workspace"
        _checkout(bugsinpy_root, project, bug, 1, fixed_workspace)
        (fixed_workspace / "evalplant_test.sh").write_text(
            _validation_script(fixed_workspace), encoding="utf-8"
        )
        oracle = _validate(fixed_workspace, timeout)
        (oracle_task_dir / "oracle_test.log").write_text(
            oracle.stdout, encoding="utf-8"
        )
        if oracle.returncode:
            raise ValueError("Fixed version does not pass its validation test")

    for name in (
        "bugsinpy_bug.info",
        "bugsinpy_patchfile.info",
        "bugsinpy_run_test.sh",
    ):
        source = workspace / name
        if source.exists():
            shutil.copy2(str(source), str(oracle_task_dir / name))
            source.unlink()
    git_dir = workspace / ".git"
    if git_dir.exists():
        shutil.rmtree(str(git_dir))

    _run(["git", "init", "-q"], cwd=workspace)
    info_exclude = workspace / ".git" / "info" / "exclude"
    with info_exclude.open("a", encoding="utf-8") as handle:
        handle.write("\nenv/\n.evalplant/\n.evalplant-python/\n")
    for command in (
        ["git", "config", "user.name", "EvalPlant"],
        ["git", "config", "user.email", "evalplant@localhost"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "buggy baseline"],
    ):
        result = _run(command, cwd=workspace)
        if result.returncode:
            raise RuntimeError(
                "Failed to create sanitized baseline:\n%s" % result.stdout
            )

    metadata = {
        "task_id": "%s-%s" % (project, bug),
        "project": project,
        "bug": bug,
        "workspace": str(workspace.resolve()),
        "task": TASK_TEMPLATE,
    }
    private_dir = workspace / ".evalplant"
    private_dir.mkdir(exist_ok=True)
    (private_dir / "task.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    shutil.copy2(
        str(oracle_task_dir / "baseline_test.log"),
        str(private_dir / "baseline_test.log"),
    )
    return metadata


def run_agent(
    workspace: Path,
    run_dir: Path,
    model: str,
    timeout: int,
    allow_host_execution: bool,
    step_limit: int = 0,
) -> Path:
    if not allow_host_execution:
        raise ValueError(
            "Refusing local agent execution without --allow-host-execution"
        )
    if step_limit < 0:
        raise ValueError("step_limit must be zero or greater")
    status = _run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=workspace
    )
    if status.returncode:
        raise RuntimeError("Could not inspect workspace:\n%s" % status.stdout)
    if status.stdout.strip():
        raise ValueError("Workspace must be clean before an agent run: %s" % workspace)
    task_data = json.loads(
        (workspace / ".evalplant" / "task.json").read_text(encoding="utf-8")
    )
    trusted_validation_script = (workspace / "evalplant_test.sh").read_text(
        encoding="utf-8"
    )
    task_id = task_data["task_id"]
    task_dir = run_dir / task_id
    if task_dir.exists() and any(task_dir.iterdir()):
        raise ValueError("Run output already exists: %s" % task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    trajectory = task_dir / "trajectory.traj.json"
    mini = shutil.which("mini")
    command = (
        [mini]
        if mini
        else [shutil.which("uvx") or "uvx", "--from", "mini-swe-agent", "mini"]
    )
    command += [
        "-m",
        model,
        "-c",
        "mini.yaml",
        "-c",
        'model.model_kwargs.thinking={"type":"disabled"}',
        "-t",
        task_data["task"],
        "-y",
        "--exit-immediately",
        "-o",
        str(trajectory.resolve()),
    ]
    if step_limit:
        command += ["-c", "agent.step_limit=%s" % step_limit]
    agent_log = task_dir / "agent.log"
    try:
        agent_env = os.environ.copy()
        agent_env["MSWEA_CONFIGURED"] = "true"
        agent = _run(
            command,
            cwd=workspace,
            timeout=timeout,
            env=agent_env,
            stdin=subprocess.DEVNULL,
        )
        agent_log.write_text(agent.stdout, encoding="utf-8")
        agent_timed_out = False
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        agent_log.write_text(output, encoding="utf-8")
        agent = None
        agent_timed_out = True

    patch = _run(["git", "diff", "--", "."], cwd=workspace)
    (task_dir / "final.patch").write_text(patch.stdout, encoding="utf-8")
    changed = _run(["git", "diff", "--name-only"], cwd=workspace)
    changed_paths = [
        line.strip() for line in changed.stdout.splitlines() if line.strip()
    ]
    integrity_violations = [
        path
        for path in changed_paths
        if path == "evalplant_test.sh" or _is_test_path(path)
    ]
    verification = _validate(
        workspace, timeout=min(timeout, 1800), trusted_script=trusted_validation_script
    )
    (task_dir / "final_test.log").write_text(verification.stdout, encoding="utf-8")
    baseline = workspace / ".evalplant" / "baseline_test.log"
    if baseline.exists():
        shutil.copy2(str(baseline), str(task_dir / "baseline_test.log"))

    if agent_timed_out:
        status = "TIMEOUT"
    elif agent is None or agent.returncode or not trajectory.exists():
        status = "INFRA_ERROR"
    else:
        status = (
            "PASS"
            if verification.returncode == 0 and not integrity_violations
            else "FAIL"
        )
    (task_dir / "verdict.json").write_text(
        json.dumps(
            {
                "status": status,
                "test_exit_code": verification.returncode,
                "integrity_violations": integrity_violations,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return task_dir


def _docker_agent_command(
    workspace: Path,
    run_dir: Path,
    image: str,
    model: str,
    timeout: int,
    step_limit: int,
    platform: str,
) -> List[str]:
    for path in (workspace, run_dir):
        if "," in str(path):
            raise ValueError("Docker bind paths cannot contain commas: %s" % path)
    command = [
        shutil.which("docker") or "docker",
        "run",
        "--rm",
        "--platform",
        platform,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,size=1g",
        "-e",
        "DEEPSEEK_API_KEY",
        "-e",
        "HOME=/tmp/evalplant-home",
        "--mount",
        "type=bind,src=%s,dst=%s" % (workspace, workspace),
        "--mount",
        "type=bind,src=%s,dst=/output" % run_dir,
        "-w",
        str(workspace),
    ]
    if hasattr(os, "getuid"):
        command += ["--user", "%s:%s" % (os.getuid(), os.getgid())]
    command += [
        image,
        "--db",
        "/tmp/evalplant.db",
        "execute",
        str(workspace),
        "--run-dir",
        "/output",
        "--model",
        model,
        "--timeout",
        str(timeout),
    ]
    if step_limit:
        command += ["--step-limit", str(step_limit)]
    return command


def run_agent_in_docker(
    workspace: Path,
    run_dir: Path,
    image: str,
    model: str,
    timeout: int,
    step_limit: int,
    platform: str,
) -> Path:
    if not shutil.which("docker"):
        raise ValueError("docker is not installed or not on PATH")
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise ValueError("DEEPSEEK_API_KEY is required")
    if step_limit < 0:
        raise ValueError("step_limit must be zero or greater")
    task_file = workspace / ".evalplant" / "task.json"
    if not task_file.exists():
        raise ValueError("Workspace is not prepared: %s" % workspace)
    task_id = json.loads(task_file.read_text(encoding="utf-8"))["task_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    task_dir = run_dir / task_id
    if task_dir.exists() and any(task_dir.iterdir()):
        raise ValueError("Run output already exists: %s" % task_dir)
    try:
        result = _run(
            _docker_agent_command(
                workspace,
                run_dir,
                image,
                model,
                timeout,
                step_limit,
                platform,
            ),
            timeout=timeout + 120,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Agent container timed out") from error
    if result.returncode:
        raise RuntimeError("Agent container failed:\n%s" % result.stdout)
    image_info = _run(
        [shutil.which("docker") or "docker", "image", "inspect", "--format={{.Id}}", image]
    )
    verdict_path = task_dir / "verdict.json"
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    verdict["runner"] = {
        "image": image,
        "image_id": image_info.stdout.strip() if image_info.returncode == 0 else None,
        "platform": platform,
        "isolated_task_mount": True,
    }
    verdict_path.write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    return task_dir
