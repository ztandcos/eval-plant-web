"""Evaluation-suite orchestration built on the existing Harbor/SQLite pipeline."""

from __future__ import annotations

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
    "COMPARING",
    "DIAGNOSING",
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
    return {
        "name": name,
        "agent": agent,
        "model": value.get("model"),
        "agent_kwargs": {str(key): value for key, value in kwargs.items()},
    }


def analyze_regressions(diagnoses: List[Dict[str, Any]]) -> Dict[str, Any]:
    clusters: Counter[str] = Counter()
    violations: Counter[str] = Counter()
    for item in diagnoses:
        diagnosis = item.get("diagnosis") or {}
        code = str(
            diagnosis.get("category_code")
            or diagnosis.get("status")
            or "UNKNOWN"
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


def load_suite(path: Path) -> Dict[str, Any]:
    path = Path(path).expanduser().resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Suite YAML must contain an object")
    name = str(data.get("suite") or "").strip()
    if not name:
        raise ValueError("Suite requires a non-empty suite name")
    raw_agents = data.get("agents")
    raw_baseline = data.get("baseline")
    raw_candidates = data.get("candidates")
    if raw_agents is not None:
        if raw_baseline is not None or raw_candidates is not None:
            raise ValueError("Use either agents: or baseline/candidates, not both")
        if not isinstance(raw_agents, list) or not raw_agents:
            raise ValueError("agents must be a non-empty list")
        parsed = [
            _variant(item, "agents[%s]" % index)
            for index, item in enumerate(raw_agents)
        ]
        if len(parsed) < 2:
            raise ValueError("agents: needs at least two entries (baseline then candidates)")
        baseline, candidates = parsed[0], parsed[1:]
    else:
        if raw_baseline is None:
            raise ValueError("Suite requires baseline or agents")
        baseline = _variant(raw_baseline, "baseline")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise ValueError("Suite requires at least one candidate")
        candidates = [
            _variant(item, "candidates[%s]" % index)
            for index, item in enumerate(raw_candidates)
        ]
    names = [baseline["name"]] + [item["name"] for item in candidates]
    if len(names) != len(set(names)):
        raise ValueError("Baseline and candidate names must be unique")

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
    metrics = data.get("metrics") or ["pass@1", "cost", "latency"]
    if not isinstance(metrics, list) or not all(isinstance(item, str) for item in metrics):
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
        "pass_drop": float(
            raw_gate.get(
                "pass_drop",
                raw_gate.get("pass_at_%s_drop" % gate_k, 0.0),
            )
        ),
        "cost_increase": float(raw_gate.get("cost_increase", 0.2)),
    }
    if any(gate[key] < 0 for key in ("max_regressions", "pass_drop", "cost_increase")):
        raise ValueError("Gate thresholds must be non-negative")

    env = data.get("env") or []
    if not isinstance(env, list) or not all(isinstance(item, str) for item in env):
        raise ValueError("env must be a list of variable names")
    diagnose_mode = str(data.get("diagnose_mode") or "regressions").strip().lower()
    if diagnose_mode not in {"regressions", "all_failures"}:
        raise ValueError("diagnose_mode must be regressions or all_failures")
    return {
        "suite": name,
        "baseline": baseline,
        "candidates": candidates,
        "benchmarks": benchmarks,
        "trials": trials,
        "concurrency": concurrency,
        "sandbox": str(data.get("sandbox") or "docker"),
        "metrics": metrics,
        "metric_ks": metric_ks,
        "gate": gate,
        "env": env,
        "diagnose_mode": diagnose_mode,
        "diagnose_all_trials": bool(
            data.get("diagnose_all_trials", diagnose_mode == "all_failures")
        ),
        "judge_model": str(
            data.get("judge_model")
            or os.getenv("EVALPLANT_JUDGE_MODEL", "deepseek-v4-pro")
        ),
        "max_input_tokens": int(
            data.get("max_input_tokens", DEFAULT_MAX_INPUT_TOKENS)
        ),
    }


def get_baseline(connection: sqlite3.Connection, suite_name: str) -> Optional[Dict[str, Any]]:
    row = connection.execute(
        "SELECT * FROM suite_baselines WHERE suite_name=?", (suite_name,)
    ).fetchone()
    return dict(row) if row else None


def promote_baseline(
    connection: sqlite3.Connection,
    suite_name: str,
    experiment_id: str,
    version_name: Optional[str] = None,
) -> Dict[str, Any]:
    exists = connection.execute(
        "SELECT 1 FROM outcomes WHERE experiment_id=? LIMIT 1", (experiment_id,)
    ).fetchone()
    if not exists:
        raise ValueError("Unknown or empty experiment: %s" % experiment_id)
    version = version_name or experiment_id
    connection.execute(
        """
        INSERT INTO suite_baselines (suite_name, experiment_id, version_name, promoted_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(suite_name) DO UPDATE SET
            experiment_id=excluded.experiment_id,
            version_name=excluded.version_name,
            promoted_at=excluded.promoted_at
        """,
        (suite_name, experiment_id, version, utcnow()),
    )
    connection.commit()
    return get_baseline(connection, suite_name) or {}


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
        refresh_baseline: bool = False,
        poll_seconds: float = 2.0,
    ) -> None:
        self.connection = connection
        self.db_path = Path(db_path).expanduser().resolve()
        self.harbor_binary = harbor_binary or find_harbor_binary()
        self.refresh_baseline = refresh_baseline
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
            self.refresh_baseline = bool(
                self.progress.get("refresh_baseline", self.refresh_baseline)
            )
            self.output_dir = Path(row["report_path"]).parent if row["report_path"] else self.db_path.parent / "suite-reports"
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
            self.progress = {
                "experiments": {},
                "completed_variants": [],
                "refresh_baseline": refresh_baseline,
            }
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

    def _import_available(self, job_dir: Path, experiment: str) -> None:
        if not job_dir.exists():
            return
        try:
            sync_execution_events(self.connection, job_dir, experiment)
            import_run(self.connection, job_dir, experiment)
        except ValueError as error:
            message = str(error)
            if "No trajectory JSON files found" in message:
                return
            if "ATIF steps, messages, or trajectory" in message:
                return
            raise

    def _wait(self, process: Any, job_dir: Path, experiment: str) -> int:
        while process.poll() is None:
            self._import_available(job_dir, experiment)
            time.sleep(self.poll_seconds)
        self._import_available(job_dir, experiment)
        return process.wait()

    def _run_variant(self, variant: Mapping[str, Any], key: str) -> str:
        experiment = self.progress["experiments"].get(key)
        if not experiment:
            experiment = "%s-%s" % (self.run_id, _slug(str(variant["name"])))
            self.progress["experiments"][key] = experiment
            self._save()
        if key in self.progress["completed_variants"] and self._experiment_has_results(experiment):
            return experiment

        jobs_dir = self.db_path.parent / "jobs"
        config = build_job_config(
            agents=[str(variant["agent"])],
            benches=[str(self.config["benchmarks"][0].get("path") or self.config["benchmarks"][0]["name"])],
            sandbox=self.config["sandbox"],
            k=self.config["trials"],
            concurrency=self.config["concurrency"],
            job_name=experiment,
            jobs_dir=jobs_dir,
            model=variant.get("model"),
            env_names=self.config["env"],
            agent_kwargs=variant.get("agent_kwargs"),
        )
        config["datasets"] = self._datasets()
        job_dir = job_dir_from_config(config)
        if self._job_finished(job_dir):
            self._import_available(job_dir, experiment)
            code = 0
        elif (job_dir / "config.json").exists():
            code = self._wait(
                resume_harbor(job_dir, binary=self.harbor_binary), job_dir, experiment
            )
        else:
            config_path = write_job_config(
                config, jobs_dir / (experiment + ".evalplant.json")
            )
            code = self._wait(
                launch_harbor(config_path, binary=self.harbor_binary),
                job_dir,
                experiment,
            )
        if not self._experiment_has_results(experiment):
            raise RuntimeError(
                "Harbor exited with code %s and produced no outcomes for %s"
                % (code, experiment)
            )
        if key not in self.progress["completed_variants"]:
            self.progress["completed_variants"].append(key)
            self._save()
        return experiment

    def _compare(self, baseline: str, candidate: str) -> Dict[str, Any]:
        gate = self.config["gate"]
        gate_result = compare_experiments(
            self.connection,
            baseline,
            candidate,
            gate["k"],
            gate["cost_increase"],
            gate["max_regressions"],
            gate["pass_drop"],
        )
        metrics = {}
        for k in self.config["metric_ks"]:
            current = gate_result if k == gate["k"] else compare_experiments(
                self.connection,
                baseline,
                candidate,
                k,
                float("inf"),
                10**9,
                1.0,
            )
            metrics["pass@%s" % k] = {
                "baseline": current["baseline_metrics"]["pass_at_k"],
                "candidate": current["candidate_metrics"]["pass_at_k"],
                "delta": current["deltas"]["pass_at_k"],
            }
        triage = []
        for item in gate_result["tasks"]:
            if "INFRA_ERROR" in item["candidate_statuses"]:
                kind = "INFRA_ERROR"
            elif item["change"] == "REGRESSED":
                kind = "NEW_REGRESSION"
            elif not item["baseline_passes"] and not item["candidate_passes"]:
                kind = "KNOWN_FAILURE"
            elif item["change"] == "IMPROVED":
                kind = "IMPROVED"
            else:
                kind = "UNCHANGED"
            triage.append({**item, "triage": kind})
        agent_regressions = sum(item["triage"] == "NEW_REGRESSION" for item in triage)
        infra_errors = sum(item["triage"] == "INFRA_ERROR" for item in triage)
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
        existing = get_diagnosis(self.connection, row["id"])
        if existing:
            return json.loads(existing["report_json"])
        if row["source_schema_version"] == "harbor-result-v1":
            diagnosis = diagnose_outcome_only(
                Path(row["raw_path"]),
                row["verdict"],
                row["health_status"],
                self.config["judge_model"],
                self.config["max_input_tokens"],
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
                    self.config["judge_model"],
                    self.config["max_input_tokens"],
                )
            except Exception as error:
                diagnosis = failed_diagnosis(
                    error,
                    self.config["judge_model"],
                    self.config["max_input_tokens"],
                )
        save_diagnosis(self.connection, row["id"], diagnosis)
        return diagnosis

    def _diagnose_regressions(
        self, candidate: str, comparison: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        results = []
        kinds = (
            {"NEW_REGRESSION", "KNOWN_FAILURE", "INFRA_ERROR"}
            if self.config.get("diagnose_mode") == "all_failures"
            else {"NEW_REGRESSION"}
        )
        limit = "" if self.config.get("diagnose_all_trials") else " LIMIT 1"
        for item in comparison["triage"]:
            if item["triage"] not in kinds:
                continue
            rows = self.connection.execute(
                """
                SELECT t.* FROM trajectories t JOIN outcomes o ON o.trajectory_id=t.id
                WHERE t.experiment_id=? AND o.task_key=? AND o.status!='PASS'
                ORDER BY COALESCE(t.finished_at, ''), t.trial_name
                """
                + limit,
                (candidate, item["task_key"]),
            ).fetchall()
            for row in rows:
                results.append(
                    {
                        "task_key": item["task_key"],
                        "trajectory_id": row["id"],
                        "diagnosis": self._diagnose_row(row),
                    }
                )
        return results

    def _payload(
        self,
        baseline: str,
        candidates: Dict[str, str],
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
            "baseline": {
                "version": self.config["baseline"]["name"],
                "experiment": baseline,
                "statistics": report(self.connection, baseline),
            },
            "candidates": {
                name: {
                    "experiment": experiment,
                    "statistics": report(self.connection, experiment),
                    "comparison": comparisons[name],
                    "diagnoses": diagnoses.get(name, []),
                }
                for name, experiment in candidates.items()
            },
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
            "| Candidate | Metrics | Improved | Regressed | Unchanged | Cost | Latency | Gate |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for name, value in payload["candidates"].items():
            comparison = value["comparison"]
            metrics = ", ".join(
                "%s %s → %s"
                % (key, self._pct(item["baseline"]), self._pct(item["candidate"]))
                for key, item in comparison["metrics"].items()
            )
            changes = comparison["gate"]["changes"]
            deltas = comparison["gate"]["deltas"]
            lines.append(
                "| %s | %s | %s | %s | %s | %s | %s | %s |"
                % (
                    name,
                    metrics,
                    changes["improved"],
                    changes["regressed"],
                    changes["unchanged"],
                    self._pct(deltas["cost_relative"]),
                    self._pct(deltas["agent_seconds_relative"]),
                    comparison["gate"]["ship_gate"]["status"],
                )
            )
        reasons = payload["ship_gate"].get("reasons") or []
        if reasons:
            lines += ["", "原因："]
            lines.extend("- %s" % reason for reason in reasons)
        lines += ["", "## Regression triage", ""]
        found = False
        index = 0
        for name, value in payload["candidates"].items():
            diagnosis_by_task = {
                item["task_key"]: item["diagnosis"] for item in value["diagnoses"]
            }
            for item in value["comparison"]["triage"]:
                if item["triage"] not in {"NEW_REGRESSION", "INFRA_ERROR"}:
                    continue
                found = True
                index += 1
                diagnosis = diagnosis_by_task.get(item["task_key"], {})
                category = (
                    diagnosis.get("category_code")
                    or diagnosis.get("status")
                    or "n/a"
                )
                label = CATEGORY_LABELS.get(str(category), str(category))
                step = diagnosis.get("root_cause_step")
                summary = str(
                    diagnosis.get("summary")
                    or (
                        "Harbor infrastructure error after retries"
                        if item["triage"] == "INFRA_ERROR"
                        else "No diagnosis"
                    )
                )
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
            lines.append("No new regressions.")
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
            lines.append("主要回归来自 %s。" % primary["label"])
            lines.append("")
            for item in payload["recommendations"]:
                lines.append(
                    "- %s (%s): %s"
                    % (item["label"], item["count"], item["action"])
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
            stored = None if self.refresh_baseline else get_baseline(
                self.connection, self.config["suite"]
            )
            if stored and self._experiment_has_results(stored["experiment_id"]):
                baseline = stored["experiment_id"]
            else:
                baseline = self._run_variant(self.config["baseline"], "baseline")
                promote_baseline(
                    self.connection,
                    self.config["suite"],
                    baseline,
                    self.config["baseline"]["name"],
                )
            candidates = {
                variant["name"]: self._run_variant(variant, "candidate:%s" % variant["name"])
                for variant in self.config["candidates"]
            }
            self.progress["baseline_experiment"] = baseline
            self._save("COLLECTING")
            self.progress["statistics"] = {
                "baseline": report(self.connection, baseline),
                **{
                    name: report(self.connection, experiment)
                    for name, experiment in candidates.items()
                },
            }
            self._save("EVALUATING")
            comparisons = {
                name: self._compare(baseline, experiment)
                for name, experiment in candidates.items()
            }
            self.progress["comparisons"] = comparisons
            self._save("COMPARING")
            self._save("DIAGNOSING")
            diagnoses = {
                name: self._diagnose_regressions(candidates[name], comparisons[name])
                for name in candidates
            }
            payload = self._payload(baseline, candidates, comparisons, diagnoses)
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
