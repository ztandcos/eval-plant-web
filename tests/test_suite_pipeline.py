import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evalplat.cli import main
from evalplat.db import connect
from evalplat.pipeline import (
    EvalPipeline,
    ensure_bootstrap_image,
    format_suite_config,
    load_suite,
    resolve_suite_path,
)
from evalplat.harbor_adapter import build_job_config
from test_pipeline import harbor_trial


class FakeProcess:
    def poll(self):
        return 0

    def wait(self):
        return 0


class FakeBuildResult:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _write_suite(root: Path, body: str) -> Path:
    (root / "tasks").mkdir(exist_ok=True)
    suite = root / "suite.yaml"
    suite.write_text(body.strip() + "\n", encoding="utf-8")
    return suite


def _two_agent_suite(root: Path, extra: str = "") -> Path:
    return _write_suite(
        root,
        """
suite: two-agent
agents:
  - name: dsh-flash
    agent: dsh
    model: deepseek/deepseek-v4-flash
    n_concurrent: 2
  - name: codex-mini
    agent: codex
    model: gpt-5-mini
    n_concurrent: 2
benchmarks:
  - path: tasks
    tasks: [kv-store-grpc, fix-git]
trials: 3
concurrency: 4
sandbox: docker
metrics: [pass@1, pass@3, cost, latency]
diagnosis:
  policy: all_final_non_pass
  judge_model: not-called
recovery:
  max_infra_retries: 1
  max_job_resumes: 1
comparison:
  baseline: dsh-flash
  candidates: [codex-mini]
gate:
  k: 1
  max_regressions: 0
  pass_at_1_drop: 0.02
  cost_increase: 0.20
%s
"""
        % extra,
    )


def _single_agent_suite(root: Path) -> Path:
    return _write_suite(
        root,
        """
suite: absolute
agents:
  - name: oracle
    agent: oracle
benchmarks:
  - path: tasks
    tasks: [task]
trials: 1
concurrency: 1
diagnosis:
  judge_model: not-called
recovery:
  max_infra_retries: 1
  max_job_resumes: 1
""",
    )


def _write_retry(job: Path, trial_name: str, exception_type: str) -> None:
    retry = job / trial_name / "_retries" / "1"
    retry.mkdir(parents=True, exist_ok=True)
    (retry / "result.json").write_text(
        json.dumps(
            {
                "id": trial_name + "-retry",
                "trial_name": trial_name,
                "task_name": "ignored",
                "exception_info": {
                    "exception_type": exception_type,
                    "exception_message": "docker compose build failed",
                },
                "agent_result": None,
                "verifier_result": None,
            }
        ),
        encoding="utf-8",
    )


def _events(job: Path, rows) -> None:
    (job / "execution-events.jsonl").write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )


class SuitePipelineTest(unittest.TestCase):
    def test_bootstrap_image_retries_once_then_succeeds(self):
        config = {
            "image": "evalplat-agent-base:local",
            "dockerfile": "/tmp/Dockerfile",
            "context": "/tmp",
            "retries": 3,
            "retry_delay_seconds": 0,
            "timeout_seconds": 60,
        }
        with patch(
            "evalplat.pipeline.subprocess.run",
            side_effect=[FakeBuildResult(1, "temporary registry error"), FakeBuildResult(0)],
        ) as build:
            ensure_bootstrap_image(config)
        self.assertEqual(build.call_count, 2)

    def _run(self, root: Path, suite: Path, launched, writer, resumes=None):
        if resumes is None:
            resumes = []

        def fake_launch(config_path, binary=None, cwd=None):
            config = json.loads(Path(config_path).read_text(encoding="utf-8"))
            launched.append(config)
            writer(config, Path(config["jobs_dir"]) / config["job_name"], "launch")
            return FakeProcess()

        def fake_resume(job_dir, binary=None, cwd=None):
            resumes.append(str(job_dir))
            if launched:
                writer(launched[-1], Path(job_dir), "resume")
            return FakeProcess()

        db = root / "evalplat.db"
        connection = connect(db)
        with patch("evalplat.pipeline.launch_harbor", fake_launch):
            with patch("evalplat.pipeline.resume_harbor", fake_resume):
                pipeline = EvalPipeline(
                    connection,
                    db,
                    suite_path=suite,
                    output_dir=root / "reports",
                    harbor_binary=root / "harbor",
                    poll_seconds=0.05,
                )
                payload = pipeline.run()
        return connection, pipeline, payload

    def test_print_config_shows_twelve_trials_without_launching(self):
        launched = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suite = _two_agent_suite(root)
            with patch("evalplat.pipeline.launch_harbor", launched.append):
                code = main(
                    [
                        "--db",
                        str(root / "evalplat.db"),
                        "eval",
                        str(suite),
                        "--print-config",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(launched, [])
            text = format_suite_config(load_suite(suite))
            self.assertIn("planned trials: 12", text)
            self.assertIn("agents: dsh-flash, codex-mini", text)
            self.assertIn("tasks: kv-store-grpc, fix-git", text)
            self.assertIn("global concurrency: 4", text)
            self.assertIn("per-agent concurrency: 2", text)
            self.assertIn("comparison: dsh-flash -> codex-mini", text)

    def test_official_demo_suite_prints_twelve_trials(self):
        config = load_suite(resolve_suite_path("terminalbench-two-agent-demo"))
        text = format_suite_config(config)
        self.assertEqual(config["trials"], 3)
        self.assertEqual(config["concurrency"], 4)
        self.assertEqual([item["n_concurrent"] for item in config["agents"]], [2, 2])
        self.assertIn("planned trials: 12", text)
        self.assertIn("comparison: dsh-flash -> codex-mini", text)

    def test_one_job_twelve_trials_and_per_agent_concurrency(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []

            def writer(config, job, _phase):
                for agent in config["agents"]:
                    for task in ("kv-store-grpc", "fix-git"):
                        for trial in range(1, 4):
                            harbor_trial(
                                job,
                                "%s__%s__%s" % (agent["name"], task, trial),
                                task,
                                reward=1.0,
                                trajectory_id="%s-%s-%s"
                                % (agent["name"], task, trial),
                                agent_name=agent["name"],
                                agent_model=agent.get("model_name"),
                            )

            connection, _, payload = self._run(
                root, _two_agent_suite(root), launched, writer
            )
            self.assertEqual(len(launched), 1)
            config = launched[0]
            self.assertEqual(config["n_concurrent_trials"], 4)
            self.assertEqual(config["retry"]["max_retries"], 1)
            self.assertEqual(
                [item["n_concurrent"] for item in config["agents"]], [2, 2]
            )
            self.assertEqual(
                [item["model_name"] for item in config["agents"]],
                ["deepseek/deepseek-v4-flash", "gpt-5-mini"],
            )
            self.assertEqual(payload["agents"]["dsh-flash"]["statistics"]["total_trials"], 6)
            self.assertEqual(payload["agents"]["codex-mini"]["statistics"]["total_trials"], 6)
            self.assertEqual(payload["ship_gate"]["status"], "PASS")
            connection.close()

    def test_agent_env_is_scoped_to_the_matching_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = load_suite(
                _write_suite(
                    root,
                    """
suite: scoped-env
agents:
  - name: codex
    agent: codex
    model: deepseek-v4-flash
    agent_env:
      OPENAI_API_KEY: ${DEEPSEEK_API_KEY}
benchmarks:
  - path: tasks
trials: 1
""",
                )
            )
            job = build_job_config(
                agents=config["agents"],
                benches=[str(root / "tasks")],
                job_name="scoped-env",
                jobs_dir=root / "jobs",
            )
            self.assertEqual(
                job["agents"][0]["env"], {"OPENAI_API_KEY": "${DEEPSEEK_API_KEY}"}
            )
            self.assertEqual(job["environment"]["env"], {})

    def test_pass_does_not_call_judge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []

            def writer(config, job, _phase):
                agent = config["agents"][0]
                harbor_trial(
                    job,
                    "pass-trial",
                    "task",
                    reward=1.0,
                    trajectory_id="pass-1",
                    agent_name=agent["name"],
                    agent_model=agent.get("model_name"),
                )

            with patch("evalplat.pipeline.analyze_trajectory") as analyze:
                with patch("evalplat.pipeline.diagnose_outcome_only") as outcome:
                    connection, _, payload = self._run(
                        root, _single_agent_suite(root), launched, writer
                    )
            analyze.assert_not_called()
            outcome.assert_not_called()
            self.assertEqual(payload["agents"]["oracle"]["diagnoses"], [])
            self.assertIsNone(payload["comparison"])
            connection.close()

    def test_fail_creates_diagnosis(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []

            def writer(config, job, _phase):
                agent = config["agents"][0]
                harbor_trial(
                    job,
                    "fail-trial",
                    "task",
                    reward=0.0,
                    completed_tool=False,
                    trajectory_id="fail-1",
                    agent_name=agent["name"],
                    agent_model=agent.get("model_name"),
                )

            connection, _, payload = self._run(
                root, _single_agent_suite(root), launched, writer
            )
            diagnoses = payload["agents"]["oracle"]["diagnoses"]
            self.assertEqual(len(diagnoses), 1)
            self.assertEqual(diagnoses[0]["diagnosis"]["category_code"], "H-T")
            connection.close()

    def test_agent_timeout_is_timeout_and_keeps_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []

            def writer(config, job, _phase):
                agent = config["agents"][0]
                harbor_trial(
                    job,
                    "timeout-trial",
                    "task",
                    completed_tool=False,
                    trajectory_id="timeout-1",
                    agent_name=agent["name"],
                    agent_model=agent.get("model_name"),
                    exception_info={
                        "exception_type": "AgentTimeoutError",
                        "exception_message": "agent reached its time limit",
                    },
                )

            connection, _, payload = self._run(
                root, _single_agent_suite(root), launched, writer
            )
            row = connection.execute("SELECT * FROM trajectories").fetchone()
            self.assertEqual(row["verdict"], "TIMEOUT")
            self.assertTrue(Path(row["raw_path"]).exists())
            self.assertEqual(len(payload["agents"]["oracle"]["diagnoses"]), 1)
            connection.close()

    def test_first_infra_failure_then_pass_has_attempt_but_no_fail_diagnosis(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []

            def writer(config, job, _phase):
                agent = config["agents"][0]
                harbor_trial(
                    job,
                    "retry-pass",
                    "task",
                    reward=1.0,
                    trajectory_id="retry-pass-1",
                    agent_name=agent["name"],
                    agent_model=agent.get("model_name"),
                )
                _write_retry(job, "retry-pass", "SandboxBuildFailedError")
                _events(
                    job,
                    [
                        {
                            "event_version": 1,
                            "job_id": "j",
                            "trial_id": "a1",
                            "trial_name": "retry-pass",
                            "task_name": "task",
                            "event": "end",
                            "state": "INFRA_ERROR",
                            "timestamp": "2026-01-01T00:00:00+00:00",
                            "retryable": True,
                            "exception_type": "SandboxBuildFailedError",
                        },
                        {
                            "event_version": 1,
                            "job_id": "j",
                            "trial_id": "a2",
                            "trial_name": "retry-pass",
                            "task_name": "task",
                            "event": "end",
                            "state": "SUCCEEDED",
                            "timestamp": "2026-01-01T00:01:00+00:00",
                        },
                    ],
                )

            connection, pipeline, payload = self._run(
                root, _single_agent_suite(root), launched, writer
            )
            row = connection.execute("SELECT * FROM trajectories").fetchone()
            self.assertEqual(row["verdict"], "PASS")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM trajectories").fetchone()[0],
                1,
            )
            self.assertEqual(payload["agents"]["oracle"]["diagnoses"], [])
            status = connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE experiment_id=?",
                (pipeline.progress["job_experiment"],),
            ).fetchone()[0]
            self.assertEqual(status, 2)
            connection.close()

    def test_two_infra_failures_are_infra_error_and_diagnosed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []

            def writer(config, job, _phase):
                agent = config["agents"][0]
                harbor_trial(
                    job,
                    "infra-fail",
                    "task",
                    trajectory_id="infra-1",
                    agent_name=agent["name"],
                    agent_model=agent.get("model_name"),
                    exception_info={
                        "exception_type": "SandboxBuildFailedError",
                        "exception_message": "Docker compose build failed",
                    },
                )
                _write_retry(job, "infra-fail", "SandboxBuildFailedError")

            connection, _, payload = self._run(
                root, _single_agent_suite(root), launched, writer
            )
            row = connection.execute("SELECT * FROM trajectories").fetchone()
            self.assertEqual(row["verdict"], "INFRA_ERROR")
            diagnosis = payload["agents"]["oracle"]["diagnoses"][0]["diagnosis"]
            self.assertEqual(diagnosis["responsibility"], "HARNESS")
            self.assertEqual(diagnosis["category_code"], "H-E")
            connection.close()

    def test_missing_verifier_is_unknown_undetermined(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []

            def writer(config, job, _phase):
                agent = config["agents"][0]
                raw = harbor_trial(
                    job,
                    "unknown-trial",
                    "task",
                    trajectory_id="unknown-1",
                    agent_name=agent["name"],
                    agent_model=agent.get("model_name"),
                )
                result_path = raw.parent.parent / "result.json"
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                payload["verifier_result"] = None
                result_path.write_text(json.dumps(payload), encoding="utf-8")

            connection, _, payload = self._run(
                root, _single_agent_suite(root), launched, writer
            )
            row = connection.execute("SELECT * FROM trajectories").fetchone()
            self.assertEqual(row["verdict"], "UNKNOWN")
            diagnosis = payload["agents"]["oracle"]["diagnoses"][0]["diagnosis"]
            self.assertEqual(diagnosis["status"], "UNDETERMINED")
            connection.close()

    def test_killed_job_becomes_incomplete_after_resume_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []
            resumes = []

            def writer(config, job, phase):
                job.mkdir(parents=True, exist_ok=True)
                (job / "config.json").write_text(json.dumps(config), encoding="utf-8")
                (job / "lock.json").write_text(
                    json.dumps(
                        {
                            "trials": [
                                {
                                    "task": {"name": "task", "source": "tasks"},
                                    "agent": {"name": "oracle"},
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                if phase == "resume":
                    return

            connection, pipeline, payload = self._run(
                root, _single_agent_suite(root), launched, writer, resumes
            )
            self.assertEqual(pipeline.progress["job_resumes"], 1)
            self.assertEqual(len(resumes), 1)
            row = connection.execute("SELECT * FROM trajectories").fetchone()
            self.assertEqual(row["verdict"], "INCOMPLETE")
            diagnosis = payload["agents"]["oracle"]["diagnoses"][0]["diagnosis"]
            self.assertEqual(diagnosis["status"], "UNDETERMINED")
            connection.close()

    def test_environment_start_timeout_is_infra_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []

            def writer(config, job, _phase):
                agent = config["agents"][0]
                harbor_trial(
                    job,
                    "env-timeout",
                    "task",
                    trajectory_id="env-1",
                    agent_name=agent["name"],
                    agent_model=agent.get("model_name"),
                    exception_info={
                        "exception_type": "EnvironmentStartTimeoutError",
                        "exception_message": "environment did not start",
                    },
                )

            connection, _, payload = self._run(
                root, _single_agent_suite(root), launched, writer
            )
            row = connection.execute("SELECT * FROM trajectories").fetchone()
            self.assertEqual(row["verdict"], "INFRA_ERROR")
            self.assertEqual(len(payload["agents"]["oracle"]["diagnoses"]), 1)
            connection.close()

    def test_verifier_timeout_is_infra_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []

            def writer(config, job, _phase):
                agent = config["agents"][0]
                harbor_trial(
                    job,
                    "verifier-timeout",
                    "task",
                    trajectory_id="vt-1",
                    agent_name=agent["name"],
                    agent_model=agent.get("model_name"),
                    exception_info={
                        "exception_type": "VerifierTimeoutError",
                        "exception_message": "verifier timed out",
                    },
                )

            connection, _, payload = self._run(
                root, _single_agent_suite(root), launched, writer
            )
            row = connection.execute("SELECT * FROM trajectories").fetchone()
            self.assertEqual(row["verdict"], "INFRA_ERROR")
            self.assertEqual(len(payload["agents"]["oracle"]["diagnoses"]), 1)
            connection.close()

    def test_comparison_labels_and_known_failure_is_still_diagnosed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []
            outcomes = {
                ("dsh-minimal", "kv-store-grpc"): 1.0,
                ("dsh-minimal", "fix-git"): 0.0,
                ("codex", "kv-store-grpc"): 0.0,
                ("codex", "fix-git"): 1.0,
            }

            def writer(config, job, _phase):
                for agent in config["agents"]:
                    for task, reward in (
                        ("kv-store-grpc", outcomes[(agent["name"], "kv-store-grpc")]),
                        ("fix-git", outcomes[(agent["name"], "fix-git")]),
                    ):
                        for trial in range(1, 4):
                            harbor_trial(
                                job,
                                "%s__%s__%s" % (agent["name"], task, trial),
                                task,
                                reward=reward,
                                completed_tool=reward == 1.0,
                                trajectory_id="%s-%s-%s"
                                % (agent["name"], task, trial),
                                agent_name=agent["name"],
                                agent_model=agent.get("model_name"),
                            )

            connection, _, payload = self._run(
                root, _two_agent_suite(root), launched, writer
            )
            labels = {
                item["task_key"]: item["triage"]
                for item in payload["comparison"]["candidates"]["codex-mini"]["triage"]
            }
            self.assertEqual(labels["kv-store-grpc"], "NEW_REGRESSION")
            self.assertEqual(labels["fix-git"], "IMPROVED")
            diagnoses = payload["agents"]["codex-mini"]["diagnoses"]
            self.assertTrue(diagnoses)
            self.assertEqual(len(launched), 1)
            connection.close()

    def test_both_pass_and_known_failure_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launched = []

            def writer(config, job, _phase):
                for agent in config["agents"]:
                    harbor_trial(
                        job,
                        "%s-pass" % agent["name"],
                        "ok",
                        reward=1.0,
                        trajectory_id="%s-pass" % agent["name"],
                        agent_name=agent["name"],
                        agent_model=agent.get("model_name"),
                    )
                    harbor_trial(
                        job,
                        "%s-fail" % agent["name"],
                        "bad",
                        reward=0.0,
                        completed_tool=False,
                        trajectory_id="%s-fail" % agent["name"],
                        agent_name=agent["name"],
                        agent_model=agent.get("model_name"),
                    )

            suite = _write_suite(
                root,
                """
suite: labels
agents:
  - name: dsh-flash
    agent: dsh
    model: deepseek/deepseek-v4-flash
  - name: codex-mini
    agent: codex
    model: gpt-5-mini
benchmarks:
  - path: tasks
    tasks: [ok, bad]
trials: 1
concurrency: 2
diagnosis:
  judge_model: not-called
comparison:
  baseline: dsh-flash
  candidates: [codex-mini]
gate:
  k: 1
  max_regressions: 9
  pass_at_1_drop: 1.0
  cost_increase: 9.0
""",
            )
            connection, _, payload = self._run(root, suite, launched, writer)
            labels = {
                item["task_key"]: item["triage"]
                for item in payload["comparison"]["candidates"]["codex-mini"]["triage"]
            }
            self.assertEqual(labels["ok"], "BOTH_PASS")
            self.assertEqual(labels["bad"], "KNOWN_FAILURE")
            self.assertTrue(payload["agents"]["codex-mini"]["diagnoses"])
            connection.close()

    def test_rejects_duplicate_harbor_identity_and_old_baseline_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suite = _write_suite(
                root,
                """
suite: dup
agents:
  - name: a
    agent: dsh
  - name: b
    agent: dsh
benchmarks:
  - path: tasks
    tasks: [task]
""",
            )
            with self.assertRaisesRegex(ValueError, "distinct Harbor agent/model"):
                load_suite(suite)
            old = _write_suite(
                root,
                """
suite: old
baseline:
  name: oracle
  agent: oracle
candidates:
  - name: dsh
    agent: dsh
benchmarks:
  - path: tasks
""",
            )
            with self.assertRaisesRegex(ValueError, "agents must contain at least one"):
                load_suite(old)

    def test_suite_paths_are_relative_to_the_yaml(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suite = _single_agent_suite(root)
            config = load_suite(suite)
            self.assertEqual(
                config["benchmarks"][0]["path"], str((root / "tasks").resolve())
            )

    def test_named_suite_resolves_under_suites(self):
        path = resolve_suite_path("smoke", cwd=Path("/tmp"))
        self.assertEqual(path.name, "smoke.yaml")
        self.assertEqual(path.parent.name, "suites")

    def test_delivery_suites_load(self):
        custom = load_suite(resolve_suite_path("custom-agent-regression"))
        self.assertEqual(custom["trials"], 3)
        self.assertEqual(custom["diagnosis"]["policy"], "all_final_non_pass")
        self.assertEqual(len(custom["benchmarks"][0]["tasks"]), 10)
        bench = load_suite(resolve_suite_path("coding-agent-regression"))
        self.assertEqual(len(bench["benchmarks"][0]["tasks"]), 12)
        self.assertIn("pass@3", bench["metrics"])

    def test_eval_cli_returns_gate_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suite = _write_suite(
                root,
                """
suite: gate-fail
agents:
  - name: dsh-flash
    agent: dsh
    model: deepseek/deepseek-v4-flash
  - name: codex-mini
    agent: codex
    model: gpt-5-mini
benchmarks:
  - path: tasks
    tasks: [task]
trials: 1
concurrency: 2
diagnosis:
  judge_model: not-called
comparison:
  baseline: dsh-flash
  candidates: [codex-mini]
gate:
  k: 1
  max_regressions: 0
  pass_at_1_drop: 0.0
  cost_increase: 9.0
""",
            )

            def fake_launch(config_path, binary=None, cwd=None):
                config = json.loads(Path(config_path).read_text(encoding="utf-8"))
                job = Path(config["jobs_dir"]) / config["job_name"]
                for agent in config["agents"]:
                    harbor_trial(
                        job,
                        agent["name"] + "-trial",
                        "task",
                        reward=1.0 if agent["name"] == "dsh-minimal" else 0.0,
                        completed_tool=agent["name"] == "dsh-minimal",
                        trajectory_id=agent["name"] + "-trial",
                        agent_name=agent["name"],
                        agent_model=agent.get("model_name"),
                    )
                return FakeProcess()

            with patch("evalplat.pipeline.launch_harbor", fake_launch):
                with patch(
                    "evalplat.pipeline.resume_harbor", lambda *args, **kwargs: FakeProcess()
                ):
                    code = main(
                        [
                            "--db",
                            str(root / "evalplat.db"),
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
            suite = _single_agent_suite(root)
            db = root / "evalplat.db"
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
