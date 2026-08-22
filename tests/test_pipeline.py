import json
import tempfile
import unittest
from pathlib import Path

from evalplant.bugsinpy import _docker_agent_command, _is_test_path, _validate
from evalplant.core import normalize_trajectory, signal_bundle
from evalplant.db import connect, import_run, save_annotation, save_attribution
from evalplant.metrics import report


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

    def test_trusted_verifier_and_test_path_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "evalplant_test.sh").write_text("exit 0\n", encoding="utf-8")
            result = _validate(workspace, 5, trusted_script="exit 7\n")
            self.assertEqual(result.returncode, 7)
            self.assertTrue(_is_test_path("tests/test_parser.py"))
            self.assertFalse(_is_test_path("src/latest.py"))

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
        mounts = [command[index + 1] for index, item in enumerate(command) if item == "--mount"]
        self.assertEqual(
            mounts,
            [
                "type=bind,src=%s,dst=%s" % (workspace, workspace),
                "type=bind,src=%s,dst=/output" % run_dir,
            ],
        )
        self.assertIn("execute", command)
        self.assertNotIn("type=bind,src=/host/evalplant,dst=/host/evalplant", mounts)


if __name__ == "__main__":
    unittest.main()
