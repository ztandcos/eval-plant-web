"""Evaluation-suite orchestration built on the existing Harbor/SQLite pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml

from .db import (
    get_diagnosis,
    import_run,
    save_diagnosis,
    sync_execution_events,
    utcnow,
)
from .harbor_adapter import (
    build_job_config,
    find_harbor_binary,
    job_dir_from_config,
    launch_harbor,
    resolve_agent,
    resolve_bench,
    resume_harbor,
    write_job_config,
)
from .judge import (
    DEFAULT_MAX_INPUT_TOKENS,
    analyze_trajectory,
    diagnose_outcome_only,
    failed_diagnosis,
)
from .metrics import ACTION_MAPPING, compare_experiments, report

STATES = (
    "CREATED",
    "RUNNING",
    "COLLECTING",
    "EVALUATING",
    "DIAGNOSING",
    "COMPARING",
    "COMPLETED",
)
REPO_ROOT = Path(__file__).resolve().parent.parent
CATEGORY_LABELS = {
    "H-E": "H-E Execution Environment（执行环境）",
    "H-T": "H-T Tool Harness（工具链路）",
    "H-C": "H-C Context Management（上下文管理）",
    "H-L": "H-L Lifecycle（生命周期）",
    "H-O": "H-O Observability（可观测性）",
    "H-V": "H-V Verification（验证判分）",
    "H-G": "H-G Governance（治理限制）",
    "L1": "L1 Goal Understanding（目标理解）",
    "L2": "L2 Reasoning（推理决策）",
    "L3": "L3 Tool Use（工具使用）",
    "L4": "L4 Verification（反馈验证）",
}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "run"


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("%s must be a positive integer" % name)
    return value


def resolve_suite_path(spec: str, *, cwd: Optional[Path] = None) -> Path:
    raw = str(spec).strip()
    if not raw:
        raise ValueError("Suite path or name is required")
    direct = Path(raw).expanduser()
    if direct.exists():
        return direct.resolve()
    name = raw if raw.endswith((".yaml", ".yml")) else raw + ".yaml"
    roots = []
    if cwd is not None:
        roots.append(Path(cwd))
    roots.append(Path.cwd())
    roots.append(REPO_ROOT)
    seen = set()
    for root in roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        for folder in (resolved / "suites" / name, resolved / name):
            if folder.exists():
                return folder.resolve()
    raise ValueError("Suite not found: %s" % spec)


def _variant(value: Any, label: str) -> Dict[str, Any]:
    if isinstance(value, str):
        name = value.strip()
        if not name:
            raise ValueError("%s cannot be empty" % label)
        return {
            "name": name,
            "agent": name,
            "model": None,
            "n_concurrent": None,
            "agent_kwargs": {},
        }
    if not isinstance(value, dict):
        raise ValueError("%s must be a string or object" % label)
    name = str(value.get("name") or "").strip()
    agent = str(value.get("agent") or name).strip()
    if not name or not agent:
        raise ValueError("%s requires name and agent" % label)
    kwargs = value.get("agent_kwargs") or {}
    if not isinstance(kwargs, dict):
        raise ValueError("%s.agent_kwargs must be an object" % label)
    n_concurrent = value.get("n_concurrent")
    if n_concurrent is not None:
        n_concurrent = _positive_int(n_concurrent, "%s.n_concurrent" % label)
    return {
        "name": name,
        "agent": agent,
        "model": value.get("model"),
        "n_concurrent": n_concurrent,
        "agent_kwargs": {str(key): value for key, value in kwargs.items()},
    }


def analyze_regressions(diagnoses: List[Dict[str, Any]]) -> Dict[str, Any]:
    clusters: Counter[str] = Counter()
    violations: Counter[str] = Counter()
    for item in diagnoses:
        diagnosis = item.get("diagnosis") or {}
        code = str(
            diagnosis.get("category_code") or diagnosis.get("status") or "UNKNOWN"
        )
        clusters[code] += 1
        primary = diagnosis.get("primary_cause") or {}
        violation = primary.get("contract_violation") or diagnosis.get(
            "contract_violation"
        )
        if violation:
            violations[str(violation)] += 1
    recommendations = [
        {
            "category": code,
            "label": CATEGORY_LABELS.get(code, code),
            "count": count,
            "action": ACTION_MAPPING[code],
        }
        for code, count in clusters.most_common()
        if code in ACTION_MAPPING
    ]
    top = clusters.most_common(1)
    return {
        "clusters": dict(clusters.most_common()),
        "contract_violations": dict(violations.most_common()),
        "primary_category": top[0][0] if top else None,
        "recommendations": recommendations,
    }


def task_count(config: Mapping[str, Any]) -> int:
    tasks = 0
    for benchmark in config["benchmarks"]:
        selected = benchmark.get("tasks")
        if isinstance(selected, list):
            tasks += len(selected)
        elif isinstance(selected, int):
            tasks += selected
    return tasks


def planned_trial_count(config: Mapping[str, Any]) -> int:
    return task_count(config) * int(config["trials"]) * len(config["agents"])


def format_suite_config(config: Mapping[str, Any]) -> str:
    agents = config["agents"]
    tasks: List[str] = []
    for benchmark in config["benchmarks"]:
        selected = benchmark.get("tasks")
        if isinstance(selected, list):
            tasks.extend(str(item) for item in selected)
    concurrent = [
        item["n_concurrent"] for item in agents if item.get("n_concurrent") is not None
    ]
    if concurrent and len(set(concurrent)) == 1:
        per_agent = str(concurrent[0])
    elif concurrent:
        per_agent = ", ".join(
            "%s=%s" % (item["name"], item["n_concurrent"] or "global")
            for item in agents
        )
    else:
        per_agent = "global"
    comparison = config.get("comparison")
    if comparison:
        comparison_line = "comparison: %s -> %s" % (
            comparison["baseline"],
            ", ".join(comparison["candidates"]),
        )
    else:
        comparison_line = "comparison: none"
    return (
        "\n".join(
            [
                "planned trials: %s" % planned_trial_count(config),
                "agents: %s" % ", ".join(item["name"] for item in agents),
                "tasks: %s" % ", ".join(tasks),
                "global concurrency: %s" % config["concurrency"],
                "per-agent concurrency: %s" % per_agent,
                comparison_line,
            ]
        )
        + "\n"
    )


def load_suite(path: Path) -> Dict[str, Any]:
    path = Path(path).expanduser().resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Suite YAML must contain an object")
    name = str(data.get("suite") or "").strip()
    if not name:
        raise ValueError("Suite requires a non-empty suite name")
    raw_agents = data.get("agents")
    if not isinstance(raw_agents, list) or not raw_agents:
        raise ValueError("agents must contain at least one entry")
    agents = [
        _variant(item, "agents[%s]" % index) for index, item in enumerate(raw_agents)
    ]
    names = [item["name"] for item in agents]
    if len(names) != len(set(names)):
        raise ValueError("Agent names must be unique")
    identities = [
        (resolved["name"], resolved.get("model_name"))
        for item in agents
        for resolved in [resolve_agent(item["agent"], item.get("model"))]
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("Each agent must have a distinct Harbor agent/model identity")

    raw_benchmarks = data.get("benchmarks")
    if not isinstance(raw_benchmarks, list) or not raw_benchmarks:
        raise ValueError("Suite requires at least one benchmark")
    benchmarks = []
    for index, raw in enumerate(raw_benchmarks):
        raw = {"name": raw} if isinstance(raw, str) else raw
        if not isinstance(raw, dict):
            raise ValueError("benchmarks[%s] must be a string or object" % index)
        name_value, path_value = raw.get("name"), raw.get("path")
        if bool(name_value) == bool(path_value):
            raise ValueError("benchmarks[%s] requires exactly one of name/path" % index)
        item: Dict[str, Any] = {
            "path": str((path.parent / str(path_value)).resolve())
            if path_value
            else None,
            "name": str(name_value) if name_value else None,
        }
        tasks = raw.get("tasks")
        if tasks is not None and not isinstance(tasks, (list, int)):
            raise ValueError("benchmarks[%s].tasks must be a list or integer" % index)
        if isinstance(tasks, int):
            _positive_int(tasks, "benchmarks[%s].tasks" % index)
        if isinstance(tasks, list) and not all(
            isinstance(task, str) and task.strip() for task in tasks
        ):
            raise ValueError("benchmarks[%s].tasks contains an invalid name" % index)
        item["tasks"] = tasks
        benchmarks.append(item)

    trials = _positive_int(data.get("trials", 1), "trials")
    concurrency = _positive_int(data.get("concurrency", 4), "concurrency")
    if any(
        item["n_concurrent"] is not None and item["n_concurrent"] > concurrency
        for item in agents
    ):
        raise ValueError("Agent n_concurrent cannot exceed global concurrency")
    metrics = data.get("metrics") or ["pass@1", "cost", "latency"]
    if not isinstance(metrics, list) or not all(
        isinstance(item, str) for item in metrics
    ):
        raise ValueError("metrics must be a list of strings")
    metric_ks = sorted(
        {
            int(match.group(1))
            for item in metrics
            for match in [re.fullmatch(r"pass@(\d+)", item.strip().lower())]
            if match
        }
    )
    if any(k < 1 or k > trials for k in metric_ks):
        raise ValueError("Every pass@k metric must satisfy 1 <= k <= trials")
    if not metric_ks:
        metric_ks = [1]
        metrics.insert(0, "pass@1")

    raw_gate = data.get("gate") or {}
    if not isinstance(raw_gate, dict):
        raise ValueError("gate must be an object")
    gate_k = _positive_int(raw_gate.get("k", 1), "gate.k")
    if gate_k > trials:
        raise ValueError("gate.k cannot exceed trials")
    gate = {
        "k": gate_k,
        "max_regressions": int(raw_gate.get("max_regressions", 0)),
        "pass_at_1_drop": float(raw_gate.get("pass_at_1_drop", 0.0)),
        "cost_increase": float(raw_gate.get("cost_increase", 0.2)),
    }
    if any(
        gate[key] < 0 for key in ("max_regressions", "pass_at_1_drop", "cost_increase")
    ):
        raise ValueError("Gate thresholds must be non-negative")

    env = data.get("env") or []
    if not isinstance(env, list) or not all(isinstance(item, str) for item in env):
        raise ValueError("env must be a list of variable names")
    raw_diagnosis = data.get("diagnosis") or {}
    if not isinstance(raw_diagnosis, dict):
        raise ValueError("diagnosis must be an object")
    policy = str(raw_diagnosis.get("policy") or "all_final_non_pass")
    if policy != "all_final_non_pass":
        raise ValueError("diagnosis.policy must be all_final_non_pass")
    raw_recovery = data.get("recovery") or {}
    if not isinstance(raw_recovery, dict):
        raise ValueError("recovery must be an object")
    recovery = {
        "max_infra_retries": int(raw_recovery.get("max_infra_retries", 1)),
        "max_job_resumes": int(raw_recovery.get("max_job_resumes", 1)),
    }
    if any(value < 0 for value in recovery.values()):
        raise ValueError("Recovery limits must be non-negative")
    raw_comparison = data.get("comparison")
    comparison = None
    if raw_comparison is not None:
        if not isinstance(raw_comparison, dict):
            raise ValueError("comparison must be an object")
        comparison = {
            "baseline": str(raw_comparison.get("baseline") or "").strip(),
            "candidates": raw_comparison.get("candidates"),
        }
        if not comparison["baseline"] or not isinstance(comparison["candidates"], list):
            raise ValueError("comparison requires baseline and candidates")
        if not comparison["candidates"] or not all(
            isinstance(item, str) and item.strip() for item in comparison["candidates"]
        ):
            raise ValueError("comparison.candidates must be a non-empty name list")
        comparison["candidates"] = [item.strip() for item in comparison["candidates"]]
        compared = [comparison["baseline"]] + comparison["candidates"]
        if len(compared) != len(set(compared)) or any(
            item not in names for item in compared
        ):
            raise ValueError("comparison names must be unique configured agents")
    return {
        "suite": name,
        "agents": agents,
        "comparison": comparison,
        "benchmarks": benchmarks,
        "trials": trials,
        "concurrency": concurrency,
        "sandbox": str(data.get("sandbox") or "docker"),
        "metrics": metrics,
        "metric_ks": metric_ks,
        "gate": gate,
        "env": env,
        "diagnosis": {
            "policy": policy,
            "judge_model": str(
                raw_diagnosis.get("judge_model")
                or os.getenv("EVALPLAT_JUDGE_MODEL", "deepseek-v4-pro")
            ),
            "max_input_tokens": int(
                raw_diagnosis.get("max_input_tokens", DEFAULT_MAX_INPUT_TOKENS)
            ),
        },
        "recovery": recovery,
    }


class EvalPipeline:
    def __init__(
        self,
        connection: sqlite3.Connection,
        db_path: Path,
        *,
        suite_path: Optional[Path] = None,
        run_id: Optional[str] = None,
        output_dir: Optional[Path] = None,
        harbor_binary: Optional[Path] = None,
        poll_seconds: float = 2.0,
    ) -> None:
        self.connection = connection
        self.db_path = Path(db_path).expanduser().resolve()
        self.harbor_binary = harbor_binary or find_harbor_binary()
        self.poll_seconds = max(0.05, poll_seconds)
        if run_id:
            row = connection.execute(
                "SELECT * FROM suite_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not row:
                raise ValueError("Unknown suite run: %s" % run_id)
            self.run_id = run_id
            self.config = json.loads(row["config_json"])
            self.progress = json.loads(row["progress_json"])
            self.output_dir = (
                Path(row["report_path"]).parent
                if row["report_path"]
                else self.db_path.parent / "suite-reports"
            )
        else:
            if suite_path is None:
                raise ValueError("suite_path is required for a new run")
            self.config = load_suite(suite_path)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            self.run_id = "%s-%s" % (_slug(self.config["suite"]), stamp)
            self.output_dir = (
                Path(output_dir).expanduser().resolve()
                if output_dir
                else self.db_path.parent / "suite-reports"
            )
            report_path = self.output_dir / (self.run_id + ".json")
            self.progress = {"experiments": {}, "job_resumes": 0}
            now = utcnow()
            connection.execute(
                """
                INSERT INTO suite_runs (
                    id, suite_name, config_path, config_json, state, progress_json,
                    report_path, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'CREATED', ?, ?, NULL, ?, ?)
                """,
                (
                    self.run_id,
                    self.config["suite"],
                    str(Path(suite_path).expanduser().resolve()),
                    json.dumps(self.config, ensure_ascii=False),
                    json.dumps(self.progress, ensure_ascii=False),
                    str(report_path),
                    now,
                    now,
                ),
            )
            connection.commit()

    def _save(self, state: Optional[str] = None, error: Optional[str] = None) -> None:
        if state is not None and state not in STATES:
            raise ValueError("Unknown pipeline state: %s" % state)
        self.connection.execute(
            """
            UPDATE suite_runs SET state=COALESCE(?, state), progress_json=?,
                error=?, updated_at=? WHERE id=?
            """,
            (
                state,
                json.dumps(self.progress, ensure_ascii=False),
                error,
                utcnow(),
                self.run_id,
            ),
        )
        self.connection.commit()

    def _datasets(self) -> List[Dict[str, Any]]:
        datasets = []
        for item in self.config["benchmarks"]:
            dataset = resolve_bench(item.get("path") or item["name"])
            tasks = item.get("tasks")
            if isinstance(tasks, list):
                dataset["task_names"] = tasks
            elif isinstance(tasks, int):
                dataset["n_tasks"] = tasks
            datasets.append(dataset)
        return datasets

    def _experiments(self) -> Dict[str, str]:
        experiments = self.progress.setdefault("experiments", {})
        for agent in self.config["agents"]:
            experiments.setdefault(
                agent["name"], "%s-%s" % (self.run_id, _slug(agent["name"]))
            )
        self.progress.setdefault("job_experiment", self.run_id + "-job")
        self._save()
        return experiments

    @staticmethod
    def _resolved_agent(agent: Mapping[str, Any]) -> Dict[str, Any]:
        return resolve_agent(str(agent["agent"]), agent.get("model"))

    def _agent_lookup(self) -> Dict[tuple, str]:
        lookup: Dict[tuple, str] = {}
        for agent in self.config["agents"]:
            resolved = self._resolved_agent(agent)
            name = resolved["name"]
            model = resolved.get("model_name")
            lookup[(name, model)] = agent["name"]
            if isinstance(model, str) and "/" in model:
                lookup[(name, model.rsplit("/", 1)[-1])] = agent["name"]
            lookup[(name, None)] = agent["name"]
        return lookup

    def _match_display_agent(self, agent: Mapping[str, Any]) -> Optional[str]:
        lookup = self._agent_lookup()
        name = agent.get("name")
        model = agent.get("model_name")
        for key in ((name, model), (name, None)):
            if key in lookup:
                return lookup[key]
        if isinstance(model, str) and "/" in model:
            return lookup.get((name, model.rsplit("/", 1)[-1]))
        if isinstance(model, str):
            for (agent_name, agent_model), display in lookup.items():
                if agent_name == name and isinstance(agent_model, str):
                    if agent_model.rsplit("/", 1)[-1] == model:
                        return display
        return None

    def _experiment_has_results(self, experiment: str) -> bool:
        return bool(
            self.connection.execute(
                "SELECT 1 FROM outcomes WHERE experiment_id=? LIMIT 1", (experiment,)
            ).fetchone()
        )

    def _job_finished(self, job_dir: Path) -> bool:
        result_path = job_dir / "result.json"
        if not result_path.exists():
            return False
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not data.get("finished_at"):
            return False
        stats = data.get("stats") or {}
        return int(stats.get("n_running_trials") or 0) == 0 and int(
            stats.get("n_pending_trials") or 0
        ) == 0

    def _import_available(self, job_dir: Path) -> None:
        if not job_dir.exists():
            return
        experiments = self._experiments()
        try:
            sync_execution_events(
                self.connection, job_dir, self.progress["job_experiment"]
            )
        except ValueError as error:
            if "execution event" not in str(error).lower():
                raise
        for agent in self.config["agents"]:
            resolved = self._resolved_agent(agent)
            try:
                import_run(
                    self.connection,
                    job_dir,
                    experiments[agent["name"]],
                    agent_model=agent.get("model"),
                    agent_name=resolved["name"],
                    model_name=resolved.get("model_name"),
                )
            except ValueError as error:
                message = str(error)
                if "No trajectory JSON files found" not in message and (
                    "ATIF steps, messages, or trajectory" not in message
                ):
                    raise

    def _wait(self, process: Any, job_dir: Path) -> int:
        while process.poll() is None:
            self._import_available(job_dir)
            time.sleep(self.poll_seconds)
        self._import_available(job_dir)
        return process.wait()

    def _planned_per_agent(self) -> int:
        expected = task_count(self.config) * self.config["trials"]
        return expected

    def _all_results_present(self) -> bool:
        expected = self._planned_per_agent()
        if not expected:
            return False
        return all(
            self.connection.execute(
                "SELECT COUNT(*) FROM outcomes WHERE experiment_id=?", (experiment,)
            ).fetchone()[0]
            >= expected
            for experiment in self._experiments().values()
        )

    def _planned_trials(self) -> List[Dict[str, Any]]:
        plans = []
        for agent in self.config["agents"]:
            resolved = self._resolved_agent(agent)
            for benchmark in self.config["benchmarks"]:
                tasks = benchmark.get("tasks") or []
                if isinstance(tasks, int):
                    continue
                for task in tasks:
                    for _ in range(self.config["trials"]):
                        plans.append(
                            {
                                "task": {
                                    "name": task,
                                    "source": benchmark.get("name")
                                    or benchmark.get("path"),
                                },
                                "agent": resolved,
                            }
                        )
        return plans

    def _record_incomplete_trials(self, job_dir: Path) -> None:
        lock_path = job_dir / "lock.json"
        plans = []
        if lock_path.exists():
            plans = json.loads(lock_path.read_text(encoding="utf-8")).get("trials") or []
        if not plans:
            plans = self._planned_trials()
        existing = {
            (name, row["source_instance_id"]): int(row["count"])
            for name, experiment in self._experiments().items()
            for row in self.connection.execute(
                """
                SELECT source_instance_id, COUNT(*) count FROM trajectories
                WHERE experiment_id=? GROUP BY source_instance_id
                """,
                (experiment,),
            ).fetchall()
        }
        seen: Counter[tuple] = Counter()
        for plan in plans:
            task = plan.get("task") or {}
            agent = plan.get("agent") or {}
            display_name = self._match_display_agent(agent)
            if display_name is None:
                continue
            key = (display_name, task.get("name"))
            seen[key] += 1
            if seen[key] <= existing.get(key, 0):
                continue
            token = hashlib.sha256(
                (self.run_id + repr(key) + str(seen[key])).encode("utf-8")
            ).hexdigest()[:16]
            record_dir = job_dir / "evalplat-incomplete" / token
            record_dir.mkdir(parents=True, exist_ok=True)
            (record_dir / "result.json").write_text(
                json.dumps(
                    {
                        "id": token,
                        "trial_name": "%s__incomplete_%s"
                        % (task.get("name"), seen[key]),
                        "task_name": task.get("name"),
                        "source": task.get("source"),
                        "config": {"agent": agent, "task": task},
                        "agent_info": {
                            "name": agent.get("name"),
                            "model_info": {"name": agent.get("model_name")},
                        },
                        "agent_result": None,
                        "verifier_result": None,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        self._import_available(job_dir)

    def _run_job(self) -> Dict[str, str]:
        experiments = self._experiments()
        jobs_dir = self.db_path.parent / "jobs"
        benches = [
            str(item.get("path") or item["name"]) for item in self.config["benchmarks"]
        ]
        config = build_job_config(
            agents=self.config["agents"],
            benches=benches,
            sandbox=self.config["sandbox"],
            k=self.config["trials"],
            concurrency=self.config["concurrency"],
            job_name=self.run_id,
            jobs_dir=jobs_dir,
            env_names=self.config["env"],
            max_infra_retries=self.config["recovery"]["max_infra_retries"],
        )
        config["datasets"] = self._datasets()
        job_dir = job_dir_from_config(config)
        launched = self.progress.setdefault("harbor_launched", False)
        if self._job_finished(job_dir):
            self._import_available(job_dir)
        else:
            if not launched and not (job_dir / "config.json").exists():
                config_path = write_job_config(
                    config, jobs_dir / (self.run_id + ".evalplat.json")
                )
                self.progress["harbor_launched"] = True
                self._save()
                self._wait(
                    launch_harbor(config_path, binary=self.harbor_binary), job_dir
                )
            while not self._job_finished(job_dir) and not self._all_results_present():
                if self.progress["job_resumes"] >= self.config["recovery"][
                    "max_job_resumes"
                ]:
                    break
                if not (job_dir / "config.json").exists():
                    break
                self.progress["job_resumes"] += 1
                self._save()
                self._wait(
                    resume_harbor(job_dir, binary=self.harbor_binary), job_dir
                )
            self._import_available(job_dir)
        if not self._all_results_present():
            self._record_incomplete_trials(job_dir)
        if not all(self._experiment_has_results(item) for item in experiments.values()):
            raise RuntimeError("Harbor produced no outcomes for one or more agents")
        return experiments

    def _compare(self, baseline: str, candidate: str) -> Dict[str, Any]:
        gate = self.config["gate"]
        gate_result = compare_experiments(
            self.connection,
            baseline,
            candidate,
            gate["k"],
            gate["cost_increase"],
            gate["max_regressions"],
            gate["pass_at_1_drop"],
        )
        metrics = {}
        for k in self.config["metric_ks"]:
            current = (
                gate_result
                if k == gate["k"]
                else compare_experiments(
                    self.connection,
                    baseline,
                    candidate,
                    k,
                    float("inf"),
                    10**9,
                    1.0,
                )
            )
            metrics["pass@%s" % k] = {
                "baseline": current["baseline_metrics"]["pass_at_k"],
                "candidate": current["candidate_metrics"]["pass_at_k"],
                "delta": current["deltas"]["pass_at_k"],
            }
        triage = []
        for item in gate_result["tasks"]:
            baseline_pass = bool(item["baseline_passes"])
            candidate_pass = bool(item["candidate_passes"])
            if baseline_pass and not candidate_pass:
                kind = "NEW_REGRESSION"
            elif not baseline_pass and not candidate_pass:
                kind = "KNOWN_FAILURE"
            elif not baseline_pass and candidate_pass:
                kind = "IMPROVED"
            else:
                kind = "BOTH_PASS"
            triage.append({**item, "triage": kind})
        agent_regressions = sum(item["triage"] == "NEW_REGRESSION" for item in triage)
        infra_errors = sum(
            "INFRA_ERROR" in item["candidate_statuses"] for item in triage
        )
        known_failures = sum(item["triage"] == "KNOWN_FAILURE" for item in triage)
        reasons = [
            reason
            for reason in gate_result["ship_gate"]["reasons"]
            if "regressed" not in reason
        ]
        if agent_regressions > gate["max_regressions"]:
            reasons.append(
                "%s new regression(s); limit is %s"
                % (agent_regressions, gate["max_regressions"])
            )
        if infra_errors:
            reasons.append(
                "%s shared task(s) remain INFRA_ERROR after Harbor retries"
                % infra_errors
            )
        gate_result = {
            **gate_result,
            "changes": {
                **gate_result["changes"],
                "regressed": agent_regressions,
                "infra_errors": infra_errors,
                "known_failures": known_failures,
            },
            "ship_gate": {
                "status": "FAIL" if reasons else "PASS",
                "reasons": reasons,
            },
        }
        return {"gate": gate_result, "metrics": metrics, "triage": triage}

    def _diagnose_row(self, row: Any) -> Dict[str, Any]:
        if row["verdict"] == "PASS":
            return {}
        existing = get_diagnosis(self.connection, row["id"])
        if existing:
            return json.loads(existing["report_json"])
        diagnosis_config = self.config["diagnosis"]
        if row["source_schema_version"] == "harbor-result-v1":
            diagnosis = diagnose_outcome_only(
                Path(row["raw_path"]),
                row["verdict"],
                row["health_status"],
                diagnosis_config["judge_model"],
                diagnosis_config["max_input_tokens"],
            )
        else:
            try:
                log = (
                    Path(row["final_log_path"]).read_text(
                        encoding="utf-8", errors="replace"
                    )
                    if row["final_log_path"]
                    else ""
                )
                diagnosis = analyze_trajectory(
                    Path(row["raw_path"]),
                    row["verdict"],
                    row["health_status"],
                    log,
                    diagnosis_config["judge_model"],
                    diagnosis_config["max_input_tokens"],
                )
            except Exception as error:
                diagnosis = failed_diagnosis(
                    error,
                    diagnosis_config["judge_model"],
                    diagnosis_config["max_input_tokens"],
                )
        save_diagnosis(self.connection, row["id"], diagnosis)
        return diagnosis

    def _diagnose_all(self, experiment: str) -> List[Dict[str, Any]]:
        results = []
        rows = self.connection.execute(
            """
            SELECT t.*, o.task_key FROM trajectories t
            JOIN outcomes o ON o.trajectory_id=t.id
            WHERE t.experiment_id=? AND o.status!='PASS'
            ORDER BY o.task_key, COALESCE(t.finished_at, ''), t.trial_name
            """,
            (experiment,),
        ).fetchall()
        for row in rows:
            results.append(
                {
                    "task_key": row["task_key"],
                    "trajectory_id": row["id"],
                    "diagnosis": self._diagnose_row(row),
                }
            )
        return results

    def _payload(
        self,
        experiments: Dict[str, str],
        comparisons: Dict[str, Any],
        diagnoses: Dict[str, Any],
    ) -> Dict[str, Any]:
        analysis = analyze_regressions(
            [item for rows in diagnoses.values() for item in rows]
        )
        clusters = analysis["clusters"]
        gate_failures = [
            name
            for name, value in comparisons.items()
            if value["gate"]["ship_gate"]["status"] == "FAIL"
        ]
        json_path = self.output_dir / (self.run_id + ".json")
        md_path = self.output_dir / (self.run_id + ".md")
        return {
            "suite": self.config["suite"],
            "run_id": self.run_id,
            "state": "COMPLETED",
            "ship_gate": {
                "status": "FAIL" if gate_failures else "PASS",
                "failed_candidates": gate_failures,
                "reasons": [
                    reason
                    for name in gate_failures
                    for reason in comparisons[name]["gate"]["ship_gate"]["reasons"]
                ],
            },
            "agents": {
                name: {
                    "experiment": experiment,
                    "statistics": report(self.connection, experiment),
                    "diagnoses": diagnoses.get(name, []),
                }
                for name, experiment in experiments.items()
            },
            "comparison": (
                {
                    "baseline": self.config["comparison"]["baseline"],
                    "candidates": comparisons,
                }
                if self.config["comparison"]
                else None
            ),
            "failure_clusters": clusters,
            "contract_violations": analysis["contract_violations"],
            "recommendations": analysis["recommendations"],
            "reports": {"json": str(json_path), "markdown": str(md_path)},
            "generated_at": utcnow(),
        }

    @staticmethod
    def _pct(value: Optional[float]) -> str:
        return "n/a" if value is None else "%.1f%%" % (value * 100)

    def _markdown(self, payload: Dict[str, Any]) -> str:
        lines = [
            "# Agent Evaluation: %s" % payload["suite"],
            "",
            "**Ship Gate: %s**" % payload["ship_gate"]["status"],
            "",
            "| Agent | Trials | PASS | Non-PASS |",
            "|---|---:|---:|---:|",
        ]
        for name, value in payload["agents"].items():
            stats = value["statistics"]
            lines.append(
                "| %s | %s | %s | %s |"
                % (
                    name,
                    stats["total_trials"],
                    stats["successful_trials"],
                    stats["failed_trials"],
                )
            )
        reasons = payload["ship_gate"].get("reasons") or []
        if reasons:
            lines += ["", "原因："]
            lines.extend("- %s" % reason for reason in reasons)
        lines += ["", "## Comparison", ""]
        found = False
        index = 0
        comparison_payload = payload.get("comparison") or {}
        for name, comparison in comparison_payload.get("candidates", {}).items():
            diagnosis_by_task = {
                item["task_key"]: item["diagnosis"]
                for item in payload["agents"][name]["diagnoses"]
            }
            for item in comparison["triage"]:
                if item["triage"] != "NEW_REGRESSION":
                    continue
                found = True
                index += 1
                diagnosis = diagnosis_by_task.get(item["task_key"], {})
                category = (
                    diagnosis.get("category_code") or diagnosis.get("status") or "n/a"
                )
                label = CATEGORY_LABELS.get(str(category), str(category))
                step = diagnosis.get("root_cause_step")
                summary = str(diagnosis.get("summary") or "No diagnosis")
                lines += [
                    "### Regression #%s" % index,
                    "",
                    "- candidate: `%s`" % name,
                    "- task: `%s`" % item["task_key"],
                    "- triage: %s" % item["triage"],
                    "- category: %s" % label,
                    "- root cause: %s" % summary,
                    "- evidence: %s"
                    % ("step %s" % step if step is not None else "n/a"),
                    "",
                ]
        if not found:
            lines.append(
                "No comparison regressions."
                if payload.get("comparison")
                else "Comparison disabled."
            )
            lines.append("")
        if payload.get("failure_clusters"):
            lines += ["## Failure clusters", ""]
            for code, count in payload["failure_clusters"].items():
                lines.append("- %s: %s" % (CATEGORY_LABELS.get(code, code), count))
            lines.append("")
        if payload.get("contract_violations"):
            lines += ["## Contract violations", ""]
            lines.extend(
                "- %s: %s" % item for item in payload["contract_violations"].items()
            )
            lines.append("")
        if payload.get("recommendations"):
            lines += ["## Recommended actions", ""]
            primary = payload["recommendations"][0]
            lines.append("主要问题来自 %s。" % primary["label"])
            lines.append("")
            for item in payload["recommendations"]:
                lines.append(
                    "- %s (%s): %s" % (item["label"], item["count"], item["action"])
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def run(self) -> Dict[str, Any]:
        row = self.connection.execute(
            "SELECT state, report_path FROM suite_runs WHERE id=?", (self.run_id,)
        ).fetchone()
        if (
            row
            and row["state"] == "COMPLETED"
            and row["report_path"]
            and Path(row["report_path"]).exists()
        ):
            return json.loads(Path(row["report_path"]).read_text(encoding="utf-8"))
        try:
            self._save("RUNNING")
            experiments = self._run_job()
            self._save("COLLECTING")
            self.progress["statistics"] = {
                name: report(self.connection, experiment)
                for name, experiment in experiments.items()
            }
            self._save("EVALUATING")
            self._save("DIAGNOSING")
            diagnoses = {
                name: self._diagnose_all(experiment)
                for name, experiment in experiments.items()
            }
            comparisons = {}
            if self.config["comparison"]:
                self._save("COMPARING")
                baseline = experiments[self.config["comparison"]["baseline"]]
                comparisons = {
                    name: self._compare(baseline, experiments[name])
                    for name in self.config["comparison"]["candidates"]
                }
                self.progress["comparisons"] = comparisons
            payload = self._payload(experiments, comparisons, diagnoses)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            json_path = Path(payload["reports"]["json"])
            md_path = Path(payload["reports"]["markdown"])
            json_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            md_path.write_text(self._markdown(payload), encoding="utf-8")
            self.progress["report_json"] = str(json_path)
            self.progress["report_markdown"] = str(md_path)
            self._save("COMPLETED")
            return payload
        except Exception as error:
            self._save(error=str(error))
            raise
