import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evalplant.cli import main
from evalplant.db import connect
from evalplant.pipeline import (
    EvalPipeline,
    get_baseline,
    load_suite,
    promote_baseline,
    resolve_suite_path,
)
from test_pipeline import harbor_trial


class FakeProcess:
    def poll(self):
        return 0

    def wait(self):
        return 0


def _write_suite(root: Path, body: str) -> Path:
    tasks = root / "tasks"
    tasks.mkdir(exist_ok=True)
    suite = root / "suite.yaml"
    suite.write_text(body.strip() + "\n", encoding="utf-8")
    return suite


def _default_suite(root: Path) -> Path:
    return _write_suite(
        root,
        """
suite: coding-regression
baseline:
  name: oracle-v1
  agent: oracle
candidates:
  - name: dsh-v2
    agent: dsh
benchmarks:
  - path: tasks
    tasks: 1
trials: 1
concurrency: 1
metrics: [pass@1, cost, latency]
gate:
  pass_at_1_drop: 0.0
  max_regressions: 0
  cost_increase: 0.2
judge_model: not-called
""",
    )


class SuitePipelineTest(unittest.TestCase):
    def _run(
        self,
        root: Path,
        suite: Path,
        launched,
        rewards=None,
        exceptions=None,
        crash_on=None,
        **kwargs,
    ):
        rewards = rewards or {}
        exceptions = exceptions or {}
        calls = {"n": 0}

        def fake_launch(config_path, binary=None, cwd=None):
            config = json.loads(Path(config_path).read_text(encoding="utf-8"))
            launched.append(config["job_name"])
            calls["n"] += 1
            if crash_on is not None and calls["n"] == crash_on:
                raise RuntimeError("simulated crash")
            agent = config["agents"][0]["name"]
            harbor_trial(
                Path(config["jobs_dir"]) / config["job_name"],
                "task__trial",
                "demo/task",
                reward=rewards.get(agent, 1.0 if agent == "oracle" else 0.0),
                completed_tool=agent == "oracle",
                trajectory_id=config["job_name"] + "-trial",
                agent_name=agent,
                exception_info=exceptions.get(agent),
            )
            return FakeProcess()

        db = root / "evalplant.db"
        connection = connect(db)
        with patch("evalplant.pipeline.launch_harbor", fake_launch):
            pipeline = EvalPipeline(
                connection,
                db,
                suite_path=suite,
                output_dir=root / "reports",
                harbor_binary=root / "harbor",
                poll_seconds=0.05,
                **kwargs,
            )
            payload = pipeline.run()
        return connection, pipeline, payload

    def test_suite_runs_scores_compares_triages_and_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []
            connection, pipeline, payload = self._run(
                root, _default_suite(root), launched
            )
            self.assertEqual(payload["ship_gate"]["status"], "FAIL")
            self.assertTrue(payload["ship_gate"]["reasons"])
            candidate = payload["candidates"]["dsh-v2"]
            self.assertEqual(candidate["statistics"]["weighted_check_pass_rate"], 0.0)
            self.assertEqual(
                candidate["comparison"]["triage"][0]["triage"], "NEW_REGRESSION"
            )
            self.assertEqual(
                candidate["diagnoses"][0]["diagnosis"]["category_code"], "H-T"
            )
            self.assertEqual(payload["failure_clusters"]["H-T"], 1)
            self.assertEqual(payload["recommendations"][0]["category"], "H-T")
            markdown = Path(payload["reports"]["markdown"]).read_text(encoding="utf-8")
            self.assertIn("Ship Gate: FAIL", markdown)
            self.assertIn("Regression #1", markdown)
            run = connection.execute(
                "SELECT * FROM suite_runs WHERE id=?", (pipeline.run_id,)
            ).fetchone()
            self.assertEqual(run["state"], "COMPLETED")
            self.assertTrue(Path(json.loads(run["progress_json"])["report_json"]).exists())
            self.assertEqual(len(launched), 2)
            baseline = get_baseline(connection, "coding-regression")
            self.assertEqual(baseline["version_name"], "oracle-v1")
            promoted = promote_baseline(
                connection,
                "coding-regression",
                candidate["experiment"],
                "dsh-v2",
            )
            self.assertEqual(promoted["experiment_id"], candidate["experiment"])
            connection.close()

    def test_known_failures_are_not_diagnosed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []
            connection, _, payload = self._run(
                root,
                _default_suite(root),
                launched,
                rewards={"oracle": 0.0, "dsh-minimal": 0.0, "dsh": 0.0},
            )
            triage = payload["candidates"]["dsh-v2"]["comparison"]["triage"][0]
            self.assertEqual(triage["triage"], "KNOWN_FAILURE")
            self.assertEqual(payload["candidates"]["dsh-v2"]["diagnoses"], [])
            self.assertEqual(payload["ship_gate"]["status"], "PASS")
            connection.close()

    def test_infra_errors_fail_gate_without_agent_diagnosis(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []
            connection, _, payload = self._run(
                root,
                _default_suite(root),
                launched,
                exceptions={
                    "dsh-minimal": {
                        "exception_type": "NetworkConnectionError",
                        "exception_message": "sandbox connection closed",
                    }
                },
            )
            comparison = payload["candidates"]["dsh-v2"]["comparison"]
            self.assertEqual(comparison["triage"][0]["triage"], "INFRA_ERROR")
            self.assertEqual(comparison["gate"]["changes"]["regressed"], 0)
            self.assertEqual(comparison["gate"]["changes"]["infra_errors"], 1)
            self.assertEqual(payload["candidates"]["dsh-v2"]["diagnoses"], [])
            self.assertEqual(payload["ship_gate"]["status"], "FAIL")
            self.assertTrue(
                any("INFRA_ERROR" in reason for reason in payload["ship_gate"]["reasons"])
            )
            connection.close()

    def test_resume_skips_finished_variants(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suite = _default_suite(root)
            launched = []
            with self.assertRaises(RuntimeError):
                self._run(root, suite, launched, crash_on=2)
            db = root / "evalplant.db"
            connection = connect(db)
            run_id = connection.execute(
                "SELECT id FROM suite_runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()["id"]
            self.assertEqual(len(launched), 2)

            def fake_launch(config_path, binary=None, cwd=None):
                config = json.loads(Path(config_path).read_text(encoding="utf-8"))
                launched.append(config["job_name"])
                agent = config["agents"][0]["name"]
                harbor_trial(
                    Path(config["jobs_dir"]) / config["job_name"],
                    "task__trial",
                    "demo/task",
                    reward=0.0,
                    completed_tool=False,
                    trajectory_id=config["job_name"] + "-trial",
                    agent_name=agent,
                )
                return FakeProcess()

            with patch("evalplant.pipeline.launch_harbor", fake_launch):
                payload = EvalPipeline(
                    connection,
                    db,
                    run_id=run_id,
                    harbor_binary=root / "harbor",
                    poll_seconds=0.05,
                ).run()
            self.assertEqual(payload["ship_gate"]["status"], "FAIL")
            self.assertEqual(len(launched), 3)
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM suite_runs WHERE id=?", (run_id,)
                ).fetchone()["state"],
                "COMPLETED",
            )
            connection.close()

    def test_second_run_reuses_production_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suite = _default_suite(root)
            launched = []
            connection, _, first = self._run(root, suite, launched)
            connection.close()
            connection, _, second = self._run(root, suite, launched)
            self.assertEqual(len(launched), 3)
            self.assertEqual(
                first["baseline"]["experiment"], second["baseline"]["experiment"]
            )
            connection.close()

    def test_suite_paths_are_relative_to_the_yaml(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suite = _default_suite(root)
            config = load_suite(suite)
            self.assertEqual(
                config["benchmarks"][0]["path"], str((root / "tasks").resolve())
            )

    def test_agents_list_is_baseline_then_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suite = _write_suite(
                root,
                """
suite: matrix
agents: [oracle, dsh]
benchmarks:
  - path: tasks
trials: 1
""",
            )
            config = load_suite(suite)
            self.assertEqual(config["baseline"]["agent"], "oracle")
            self.assertEqual(config["candidates"][0]["agent"], "dsh")

    def test_named_suite_resolves_under_suites(self):
        path = resolve_suite_path("smoke", cwd=Path("/tmp"))
        self.assertEqual(path.name, "smoke.yaml")
        self.assertEqual(path.parent.name, "suites")

    def test_delivery_suites_load(self):
        custom = load_suite(resolve_suite_path("custom-agent-regression"))
        self.assertEqual(custom["trials"], 3)
        self.assertEqual(custom["diagnose_mode"], "all_failures")
        self.assertEqual(len(custom["benchmarks"][0]["tasks"]), 10)
        bench = load_suite(resolve_suite_path("coding-agent-regression"))
        self.assertEqual(len(bench["benchmarks"][0]["tasks"]), 12)
        self.assertIn("pass@3", bench["metrics"])

    def test_eval_cli_returns_gate_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suite = _default_suite(root)

            def fake_launch(config_path, binary=None, cwd=None):
                config = json.loads(Path(config_path).read_text(encoding="utf-8"))
                agent = config["agents"][0]["name"]
                harbor_trial(
                    Path(config["jobs_dir"]) / config["job_name"],
                    "task__trial",
                    "demo/task",
                    reward=1.0 if agent == "oracle" else 0.0,
                    completed_tool=agent == "oracle",
                    trajectory_id=config["job_name"] + "-trial",
                    agent_name=agent,
                )
                return FakeProcess()

            with patch("evalplant.pipeline.launch_harbor", fake_launch):
                code = main(
                    [
                        "--db",
                        str(root / "evalplant.db"),
                        "eval",
                        str(suite),
                        "--output-dir",
                        str(root / "reports"),
                        "--harbor",
                        str(root / "harbor"),
                        "--poll-seconds",
                        "0.05",
                    ]
                )
            self.assertEqual(code, 1)

    def test_partial_job_result_is_not_treated_as_finished(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suite = _default_suite(root)
            db = root / "evalplant.db"
            connection = connect(db)
            pipeline = EvalPipeline(
                connection,
                db,
                suite_path=suite,
                harbor_binary=root / "harbor",
                poll_seconds=0.05,
            )
            job = root / "job"
            job.mkdir()
            (job / "result.json").write_text(
                json.dumps(
                    {
                        "finished_at": "2026-08-27T13:00:00Z",
                        "stats": {"n_running_trials": 3, "n_pending_trials": 15},
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(pipeline._job_finished(job))
            (job / "result.json").write_text(
                json.dumps(
                    {
                        "finished_at": "2026-08-27T13:00:00Z",
                        "stats": {"n_running_trials": 0, "n_pending_trials": 0},
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(pipeline._job_finished(job))
            connection.close()


if __name__ == "__main__":
    unittest.main()
