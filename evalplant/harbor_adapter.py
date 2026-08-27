"""Drive Harbor as an installed tool, not a nested project.

EvalPlant owns agent / bench / sandbox aliases and writes a Harbor JobConfig
JSON. The optional local ``harbor/`` checkout is only for patch development.
"""

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
    "HealthcheckError",
    "SandboxBuildFailedError",
    "NetworkConnectionError",
    "ApiInternalServerError",
    "ApiOverloadedError",
    "ApiConnectionClosedError",
    "ApiResponseStalledError",
)

KNOWN_SECRET_ENV = (
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
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
    names = list(KNOWN_SECRET_ENV)
    if extra:
        names.extend(item.strip() for item in extra if item and item.strip())
    templates = {}
    for name in names:
        if name in os.environ and os.environ[name] != "":
            templates[name] = "${%s}" % name
    return templates


def build_job_config(
    *,
    agents: Sequence[str],
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
) -> Dict[str, Any]:
    if not agents:
        raise ValueError("At least one --agent is required")
    if not benches:
        raise ValueError("At least one --bench is required")
    if k < 1:
        raise ValueError("--k must be >= 1")
    if concurrency < 1:
        raise ValueError("--concurrency must be >= 1")
    kind = sandbox.strip().lower()
    if kind not in SANDBOXES:
        raise ValueError(
            "Unknown sandbox %r. Expected one of: %s"
            % (sandbox, ", ".join(sorted(SANDBOXES)))
        )
    resolved_agents = [resolve_agent(name, model) for name in agents]
    if agent_kwargs:
        for agent in resolved_agents:
            merged = dict(agent.get("kwargs") or {})
            merged.update(agent_kwargs)
            agent["kwargs"] = merged
    jobs_dir = Path(jobs_dir).expanduser().resolve()
    return {
        "job_name": job_name,
        "jobs_dir": str(jobs_dir),
        "n_attempts": k,
        "n_concurrent_trials": concurrency,
        "quiet": False,
        "retry": {
            "max_retries": 2,
            "include_exceptions": list(INFRA_RETRY_EXCEPTIONS),
        },
        "environment": {
            "type": kind,
            "force_build": force_build,
            "delete": True,
            "env": env_templates(env_names),
        },
        "agents": resolved_agents,
        "datasets": [resolve_bench(item, tasks) for item in benches],
    }


def default_job_name(experiment: Optional[str] = None) -> str:
    if experiment:
        return experiment
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return "evalplant-%s" % stamp


def write_job_config(config: Mapping[str, Any], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def find_harbor_binary() -> Path:
    explicit = os.getenv("EVALPLANT_HARBOR")
    if explicit:
        binary = Path(explicit).expanduser()
        if binary.is_file():
            return binary
        raise FileNotFoundError(
            "EVALPLANT_HARBOR does not point to a file: %s" % binary
        )
    if LOCAL_HARBOR_BINARY.is_file():
        return LOCAL_HARBOR_BINARY
    found = shutil.which("harbor")
    if found:
        return Path(found)
    root = os.getenv("EVALPLANT_HARBOR_ROOT")
    if root:
        candidate = Path(root).expanduser() / ".venv" / "bin" / "harbor"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Harbor is not installed. Install the harbor package, put `harbor` on PATH, "
        "or set EVALPLANT_HARBOR to the binary. The local harbor/ checkout is optional "
        "and only used to develop integrations/harbor-patches."
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
        root = os.getenv("EVALPLANT_HARBOR_ROOT")
        workdir = Path(root).expanduser() if root else None
    return subprocess.Popen(command, cwd=str(workdir) if workdir else None)


def job_dir_from_config(config: Mapping[str, Any]) -> Path:
    return Path(config["jobs_dir"]) / config["job_name"]
