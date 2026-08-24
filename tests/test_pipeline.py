import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from evalplant.bugsinpy import (
    _docker_agent_command,
    _docker_prepare_command,
    _is_test_path,
    _validate,
)
from evalplant.core import classify_step, normalize_trajectory, signal_bundle
from evalplant.db import connect, import_run, save_annotation, save_attribution
from evalplant.metrics import compare_experiments, report
from evalplant.online import ingest_payload
from evalplant.judge import analyze_trajectory


class PipelineTest(unittest.TestCase):
    def test_import_annotate_and_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_dir = root / "runs" / "black-1"
            task_dir.mkdir(parents=True)
            trajectory = {
                "task_id": "black-1",
                "info": {"model_stats": {"instance_cost": 0.1, "api_calls": 3}},
                "messages": [
                    {"role": "user", "content": "Fix the bug"},
                    {
                        "role": "assistant",
                        "content": (
                            "I will run the test.\n```bash\npytest tests/test_x.py\n```"
                        ),
                    },
                    {"role": "tool", "content": "1 failed, 2 passed"},
                    {
                        "role": "assistant",
                        "content": (
                            "I will edit the unrelated file.\n"
                            "```bash\nsed -i '' 's/x/y/' wrong.py\n```"
                        ),
                    },
                    {"role": "exit", "content": "LimitsExceeded"},
                ],
            }
            (task_dir / "trajectory.json").write_text(
                json.dumps(trajectory), encoding="utf-8"
            )
            (task_dir / "verdict.json").write_text(
                '{"status":"FAIL"}', encoding="utf-8"
            )

            connection = connect(root / "evalplant.db")
            trajectory_id = import_run(connection, task_dir, "exp-1")[0]
            save_attribution(
                connection,
                trajectory_id,
                {
                    "attributable": True,
                    "first_error_step": 3,
                    "stage": "fault_localization",
                    "mechanism": "wrong_assumption",
                    "summary": "Edited an unrelated file",
                    "evidence_step_ids": [2, 3],
                    "confidence": 0.9,
                },
            )
            save_annotation(
                connection,
                trajectory_id,
                "test",
                3,
                "fault_localization",
                "wrong_assumption",
                [2, 3],
                True,
                "",
                True,
            )

            result = report(connection, "exp-1", "test")
            self.assertEqual(result["verdicts"], {"FAIL": 1})
            self.assertEqual(result["exact_step_accuracy"], 1.0)
            self.assertEqual(result["stage_macro_f1"], 1.0)
            self.assertEqual(result["evidence_pass_rate"], 1.0)

            steps = normalize_trajectory(trajectory)
            self.assertEqual(steps[1]["action_type"], "test_execution")
            self.assertEqual(steps[1]["command"], "pytest tests/test_x.py")
            self.assertEqual(steps[1]["test_status"], "failed")
            self.assertEqual(steps[2]["action_type"], "test_output")
            self.assertEqual(
                signal_bundle(steps)["terminal_statuses"], ["LimitsExceeded"]
            )
            connection.close()

    def test_trusted_verifier_and_test_path_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "evalplant_test.sh").write_text("exit 0\n", encoding="utf-8")
            result = _validate(workspace, 5, trusted_script="exit 7\n")
            self.assertEqual(result.returncode, 7)
            self.assertTrue(_is_test_path("tests/test_parser.py"))
            self.assertFalse(_is_test_path("src/latest.py"))
            self.assertEqual(
                classify_step("assistant", "", "./evalplant_test.sh"),
                "test_execution",
            )
            self.assertEqual(
                classify_step("assistant", "", "cat evalplant_test.sh"),
                "file_read",
            )
            self.assertEqual(
                classify_step(
                    "tool", '{"returncode": 0, "output": "error docs"}', None
                ),
                "tool_output",
            )
            self.assertEqual(
                classify_step("assistant", "", "cd /task 2>/dev/null; ls"),
                "file_read",
            )

    def test_imports_harbor_atif_with_verifier_and_raw_events(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trial = root / "job" / "hello-world__abc"
            session = trial / "agent" / "dsh-sessions" / "session-1"
            verifier = trial / "verifier"
            session.mkdir(parents=True)
            verifier.mkdir()
            atif = {
                "schema_version": "ATIF-v1.7",
                "session_id": "session-1",
                "agent": {
                    "name": "dsh-minimal",
                    "version": "0.1.0rc7",
                    "model_name": "deepseek-v4-flash",
                },
                "steps": [
                    {"step_id": 1, "source": "user", "message": "Create hello.txt"},
                    {
                        "step_id": 2,
                        "source": "agent",
                        "message": "(tool use)",
                        "tool_calls": [
                            {
                                "tool_call_id": "call-1",
                                "function_name": "bash",
                                "arguments": {"command": "printf hi > hello.txt"},
                            }
                        ],
                        "observation": {
                            "results": [{"source_call_id": "call-1", "content": ""}]
                        },
                    },
                ],
            }
            (trial / "agent" / "trajectory.json").write_text(json.dumps(atif))
            (session / "session.jsonl").write_text('{"type":"request/header"}\n')
            (verifier / "test-stdout.txt").write_text("passed\n")
            (trial / "result.json").write_text(
                json.dumps(
                    {
                        "id": "trial-id",
                        "task_name": "harbor/hello-world",
                        "trial_name": "hello-world__abc",
                        "agent_info": {
                            "name": "dsh-minimal",
                            "version": "0.1.0rc7",
                            "model_info": {"name": "deepseek-v4-flash"},
                        },
                        "agent_result": {"cost_usd": None},
                        "environment_setup": {
                            "started_at": "2026-01-01T00:00:00Z",
                            "finished_at": "2026-01-01T00:00:02Z",
                        },
                        "agent_setup": {
                            "started_at": "2026-01-01T00:00:02Z",
                            "finished_at": "2026-01-01T00:00:05Z",
                        },
                        "agent_execution": {
                            "started_at": "2026-01-01T00:00:05Z",
                            "finished_at": "2026-01-01T00:00:12Z",
                        },
                        "verifier": {
                            "started_at": "2026-01-01T00:00:12Z",
                            "finished_at": "2026-01-01T00:00:13Z",
                        },
                        "verifier_result": {"rewards": {"reward": 1.0}},
                    }
                )
            )

            connection = connect(root / "evalplant.db")
            trajectory_id = import_run(connection, trial.parent, "harbor-smoke")[0]
            row = connection.execute(
                "SELECT * FROM trajectories WHERE id=?", (trajectory_id,)
            ).fetchone()
            self.assertEqual(row["base_task_id"], "harbor/hello-world")
            self.assertEqual(row["health_status"], "VALID")
            self.assertEqual(row["verdict"], "PASS")
            self.assertTrue(row["raw_event_sha256"])
            self.assertEqual(row["agent_execution_seconds"], 7.0)
            step = connection.execute(
                "SELECT * FROM steps WHERE trajectory_id=? AND step_index=2",
                (trajectory_id,),
            ).fetchone()
            self.assertEqual(step["action_type"], "file_edit")
            self.assertEqual(step["tool_name"], "bash")
            self.assertEqual(
                report(connection, "harbor-smoke", "test")["average_reward"], 1.0
            )
            comparison = compare_experiments(connection, "harbor-smoke", "harbor-smoke")
            self.assertEqual(comparison["tasks"][0]["steps_a"], 2.0)
            connection.close()

    def test_online_known_failure_is_queued(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = ingest_payload(
                root / "evalplant.db",
                root / "online",
                {
                    "experiment": "online-shadow",
                    "trajectory": {
                        "schema_version": "ATIF-v1.7",
                        "session_id": "session-1",
                        "agent": {"name": "dsh-minimal", "version": "0.1.0rc7"},
                        "steps": [
                            {"step_id": 1, "source": "user", "message": "Fix it"}
                        ],
                    },
                    "result": {
                        "id": "online-trial",
                        "task_name": "shadow/task-1",
                        "trial_name": "task-1__one",
                        "agent_result": {"metadata": {"online": True}},
                        "verifier_result": {"rewards": {"reward": 0.0}},
                    },
                },
            )
            self.assertEqual(result["health_status"], "VALID")
            self.assertEqual(result["verdict"], "FAIL")
            self.assertTrue(result["attribution_queued"])
            connection = connect(root / "evalplant.db")
            status = connection.execute(
                "SELECT status FROM attribution_jobs WHERE trajectory_id=?",
                (result["trajectory_id"],),
            ).fetchone()["status"]
            self.assertEqual(status, "PENDING")
            connection.close()

            unknown = ingest_payload(
                root / "evalplant.db",
                root / "online",
                {
                    "experiment": "online-shadow",
                    "task_id": "shadow/task-unknown",
                    "trajectory": {
                        "schema_version": "ATIF-v1.7",
                        "session_id": "session-unknown",
                        "steps": [{"step_id": 1, "source": "user", "message": "Help"}],
                    },
                },
            )
            self.assertEqual(unknown["verdict"], "UNKNOWN")
            self.assertFalse(unknown["attribution_queued"])

    def test_attribution_uses_one_judge_call(self):
        with tempfile.TemporaryDirectory() as temp:
            trajectory = Path(temp) / "trajectory.json"
            trajectory.write_text(
                json.dumps(
                    {
                        "schema_version": "ATIF-v1.7",
                        "steps": [
                            {"step_id": 1, "source": "user", "message": "Fix it"},
                            {
                                "step_id": 2,
                                "source": "agent",
                                "message": "I will submit without tests",
                            },
                        ],
                    }
                )
            )
            create = Mock(
                return_value=SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps(
                                    {
                                        "attributable": True,
                                        "first_error_step": 2,
                                        "stage": "iterative_verification",
                                        "mechanism": "validation_retreat",
                                        "subcategory": "verification_abandonment",
                                        "summary": "Submitted without verification",
                                        "evidence_step_ids": [2],
                                        "confidence": 0.8,
                                    }
                                )
                            )
                        )
                    ]
                )
            )
            client = SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=create))
            )
            with (
                patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}),
                patch("openai.OpenAI", return_value=client),
            ):
                result = analyze_trajectory(trajectory)

            create.assert_called_once()
            self.assertTrue(result["deterministic_evidence"]["verification_missing"])

    def test_docker_agent_mounts_only_task_and_output(self):
        workspace = Path("/host/evalplant/.workspaces/fastapi-1")
        run_dir = Path("/host/evalplant/data/raw")
        command = _docker_agent_command(
            workspace,
            run_dir,
            "evalplant-agent:0.2",
            "deepseek/deepseek-v4-flash",
            900,
            20,
            "linux/amd64",
        )
        mounts = [
            command[index + 1]
            for index, item in enumerate(command)
            if item == "--mount"
        ]
        self.assertEqual(
            mounts,
            [
                "type=bind,src=%s,dst=/task" % workspace,
                "type=bind,src=%s,dst=/output" % run_dir,
            ],
        )
        self.assertIn("execute", command)
        self.assertIn("/task", command)
        self.assertNotIn("type=bind,src=/host/evalplant,dst=/host/evalplant", mounts)

    def test_docker_prepare_mounts_one_task_at_fixed_path(self):
        command = _docker_prepare_command(
            Path("/host/evalplant/.benchmarks/BugsInPy"),
            Path("/host/evalplant/.workspaces/fastapi-1"),
            Path("/host/evalplant/data/oracle"),
            "evalplant-agent:0.2",
            "fastapi",
            1,
            1800,
            "linux/amd64",
        )
        mounts = [
            command[index + 1]
            for index, item in enumerate(command)
            if item == "--mount"
        ]
        self.assertEqual(
            mounts,
            [
                "type=bind,src=/host/evalplant/.benchmarks/BugsInPy,dst=/bench",
                "type=bind,src=/host/evalplant/.workspaces/fastapi-1,dst=/task",
                "type=bind,src=/host/evalplant/data/oracle,dst=/oracle",
            ],
        )
        self.assertNotIn("type=bind,src=/host/evalplant/.workspaces,dst=/task", mounts)


if __name__ == "__main__":
    unittest.main()
