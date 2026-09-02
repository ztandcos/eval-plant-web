"""Drive the Harbor fork bundled inside the EvalPlat repository."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

LOCAL_HARBOR_BINARY = (
    Path(__file__).resolve().parent.parent / "harbor" / ".venv" / "bin" / "harbor"
)

AGENT_ALIASES: Dict[str, Dict[str, Any]] = {
    "dsh": {
        "name": "dsh-minimal",
        "kwargs": {"version": "0.1.0rc7"},
        "model_name": "deepseek/deepseek-v4-flash",
    },
    "dsh-minimal": {
        "name": "dsh-minimal",
        "kwargs": {"version": "0.1.0rc7"},
        "model_name": "deepseek/deepseek-v4-flash",
    },
    "claude-code": {"name": "claude-code"},
    "codex": {"name": "codex"},
    "openhands": {"name": "openhands"},
    "mini-swe-agent": {"name": "mini-swe-agent"},
    "terminus": {"name": "terminus"},
    "oracle": {"name": "oracle"},
    "nop": {"name": "nop"},
}

SANDBOXES = frozenset(
    {
        "docker",
        "daytona",
        "e2b",
        "modal",
        "runloop",
        "langsmith",
        "ec2",
        "gke",
        "ack",
        "openshift",
        "novita",
        "apple-container",
        "singularity",
        "islo",
        "tensorlake",
        "cwsandbox",
        "wandb",
        "use-computer",
        "cua-cloud",
        "blaxel",
        "opensandbox",
        "beam",
        "skypilot",
        "hf-sandbox",
        "hyperbrowser",
        "vercel",
    }
)

INFRA_RETRY_EXCEPTIONS = (
    "EnvironmentStartTimeoutError",
    "VerifierTimeoutError",
    "HealthcheckError",
    "SandboxBuildFailedError",
    "NetworkConnectionError",
    "ApiInternalServerError",
    "ApiOverloadedError",
    "ApiConnectionClosedError",
    "ApiResponseStalledError",
    # Harbor raises RuntimeError when `docker compose build/start` fails,
    # including flaky apt-get during image builds.
    "RuntimeError",
)

def resolve_agent(spec: str, model: Optional[str] = None) -> Dict[str, Any]:
    raw = spec.strip()
    if not raw:
        raise ValueError("Agent name is empty")
    alias = AGENT_ALIASES.get(raw.lower(), {"name": raw})
    agent = dict(alias)
    if "kwargs" in agent:
        agent["kwargs"] = dict(agent["kwargs"])
    if model:
        agent["model_name"] = model
    return agent


def resolve_bench(spec: str, tasks: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    raw = spec.strip()
    if not raw:
        raise ValueError("Bench name is empty")
    dataset: Dict[str, Any]
    path = Path(raw).expanduser()
    if path.exists():
        dataset = {"path": str(path.resolve())}
    else:
        dataset = {"name": raw}
    if tasks:
        dataset["task_names"] = list(tasks)
    return dataset


def env_templates(extra: Optional[Iterable[str]] = None) -> Dict[str, str]:
    names = [item.strip() for item in extra or () if item and item.strip()]
    templates = {}
    for name in names:
        if name in os.environ and os.environ[name] != "":
            templates[name] = "${%s}" % name
    return templates


def build_job_config(
    *,
    agents: Sequence[Any],
    benches: Sequence[str],
    sandbox: str = "docker",
    k: int = 1,
    concurrency: int = 4,
    job_name: str,
    jobs_dir: Path,
    model: Optional[str] = None,
    tasks: Optional[Sequence[str]] = None,
    env_names: Optional[Sequence[str]] = None,
    agent_kwargs: Optional[Mapping[str, Any]] = None,
    force_build: bool = False,
    max_infra_retries: int = 2,
    agent_setup_timeout_multiplier: float = 1.0,
) -> Dict[str, Any]:
    if not agents:
        raise ValueError("At least one --agent is required")
    if not benches:
        raise ValueError("At least one --bench is required")
    if k < 1:
        raise ValueError("--k must be >= 1")
    if concurrency < 1:
        raise ValueError("--concurrency must be >= 1")
    if max_infra_retries < 0:
        raise ValueError("max_infra_retries must be >= 0")
    if agent_setup_timeout_multiplier <= 0:
        raise ValueError("agent_setup_timeout_multiplier must be > 0")
    kind = sandbox.strip().lower()
    if kind not in SANDBOXES:
        raise ValueError(
            "Unknown sandbox %r. Expected one of: %s"
            % (sandbox, ", ".join(sorted(SANDBOXES)))
        )
    resolved_agents = []
    for item in agents:
        n_concurrent = concurrency
        if isinstance(item, str):
            agent = resolve_agent(item, model)
            kwargs = agent_kwargs
        elif isinstance(item, Mapping):
            agent = resolve_agent(str(item["agent"]), item.get("model"))
            kwargs = item.get("agent_kwargs")
            agent_env = item.get("agent_env") or {}
            if not isinstance(agent_env, Mapping) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in agent_env.items()
            ):
                raise ValueError("agents.agent_env must be an object of strings")
            if agent_env:
                agent["env"] = dict(agent_env)
            if item.get("n_concurrent") is not None:
                n_concurrent = int(item["n_concurrent"])
        else:
            raise ValueError("agents entries must be strings or objects")
        if kwargs:
            merged = dict(agent.get("kwargs") or {})
            merged.update(kwargs)
            agent["kwargs"] = merged
        agent["n_concurrent"] = n_concurrent
        resolved_agents.append(agent)
    jobs_dir = Path(jobs_dir).expanduser().resolve()
    return {
        "job_name": job_name,
        "jobs_dir": str(jobs_dir),
        "n_attempts": k,
        "n_concurrent_trials": concurrency,
        "quiet": False,
        "retry": {
            "max_retries": max_infra_retries,
            "include_exceptions": list(INFRA_RETRY_EXCEPTIONS),
            "exclude_exceptions": ["AgentTimeoutError"],
        },
        "environment": {
            "type": kind,
            "force_build": force_build,
            "delete": True,
            "env": env_templates(env_names),
        },
        "agents": resolved_agents,
        "datasets": [resolve_bench(item, tasks) for item in benches],
        "agent_setup_timeout_multiplier": agent_setup_timeout_multiplier,
    }


def default_job_name(experiment: Optional[str] = None) -> str:
    if experiment:
        return experiment
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return "evalplat-%s" % stamp


def write_job_config(config: Mapping[str, Any], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def find_harbor_binary() -> Path:
    explicit = os.getenv("EVALPLAT_HARBOR")
    if explicit:
        binary = Path(explicit).expanduser()
        if binary.is_file():
            return binary
        raise FileNotFoundError(
            "EVALPLAT_HARBOR does not point to a file: %s" % binary
        )
    if LOCAL_HARBOR_BINARY.is_file():
        return LOCAL_HARBOR_BINARY
    found = shutil.which("harbor")
    if found:
        return Path(found)
    root = os.getenv("EVALPLAT_HARBOR_ROOT")
    if root:
        candidate = Path(root).expanduser() / ".venv" / "bin" / "harbor"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "The bundled Harbor environment is not installed. Run `uv sync --project "
        "harbor`, put another compatible `harbor` on PATH, or set "
        "EVALPLAT_HARBOR to its binary."
    )


def launch_harbor(
    config_path: Path,
    *,
    binary: Optional[Path] = None,
    cwd: Optional[Path] = None,
) -> subprocess.Popen:
    harbor = Path(binary) if binary is not None else find_harbor_binary()
    command = [str(harbor), "run", "-c", str(config_path), "-y"]
    workdir = cwd
    if workdir is None:
        root = os.getenv("EVALPLAT_HARBOR_ROOT")
        workdir = Path(root).expanduser() if root else None
    return subprocess.Popen(command, cwd=str(workdir) if workdir else None)


def resume_harbor(
    job_dir: Path,
    *,
    binary: Optional[Path] = None,
    cwd: Optional[Path] = None,
) -> subprocess.Popen:
    harbor = Path(binary) if binary is not None else find_harbor_binary()
    command = [str(harbor), "jobs", "resume", "-p", str(job_dir)]
    return subprocess.Popen(command, cwd=str(cwd) if cwd else None)


def job_dir_from_config(config: Mapping[str, Any]) -> Path:
    return Path(config["jobs_dir"]) / config["job_name"]
