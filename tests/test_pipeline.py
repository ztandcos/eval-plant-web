import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from evalplant.cli import command_report, parser
from evalplant.core import build_segment_index, build_structured_index, estimate_tokens
from evalplant.db import (
    connect,
    execution_status,
    import_run,
    save_diagnosis,
    sync_execution_events,
)
from evalplant.evaluation import evaluate, stability
from evalplant.judge import analyze_trajectory
from evalplant.metrics import report


def harbor_run(root: Path, *, completed_tool=True, reward=0.0) -> Path:
    trial = root / "job" / "task__trial"
    agent = trial / "agent"
    verifier = trial / "verifier"
    agent.mkdir(parents=True)
    verifier.mkdir()
    steps = [
        {"step_id": 1, "source": "user", "message": "Fix the service"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "I will change config.",
            "tool_calls": [
                {
                    "tool_call_id": "call-1",
                    "function_name": "shell",
                    "arguments": {"command": "printf bad > config.ini"},
                }
            ],
        },
    ]
    if completed_tool:
        steps.append(
            {
                "step_id": 3,
                "source": "tool",
                "observation": {
                    "results": [
                        {"source_call_id": "call-1", "content": "command completed"}
                    ]
                },
            }
        )
    raw_path = agent / "trajectory.json"
    raw_path.write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "session_id": "session-1",
                "agent": {
                    "name": "dsh",
                    "version": "0.1",
                    "model_name": "deepseek",
                },
                "steps": steps,
            }
        ),
        encoding="utf-8",
    )
    (trial / "result.json").write_text(
        json.dumps(
            {
                "id": "trajectory-1",
                "task_name": "terminal-bench/task",
                "trial_name": "task__trial",
                "agent_info": {
                    "version": "0.1",
                    "model_info": {"name": "deepseek"},
                },
                "agent_result": {
                    "cost_usd": 0.2,
                    "api_calls": 2,
                    "n_input_tokens": 100,
                    "n_cache_tokens": 10,
                    "n_output_tokens": 20,
                },
                "verifier_result": {"rewards": {"task": reward}},
            }
        ),
        encoding="utf-8",
    )
    (verifier / "test-stdout.txt").write_text("1 test failed", encoding="utf-8")
    return raw_path


def mock_client(payload):
    payloads = payload if isinstance(payload, list) else [payload]
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps(item)))
            ],
            usage=SimpleNamespace(prompt_tokens=50, completion_tokens=25),
        )
        for item in payloads
    ]
    create = Mock(side_effect=responses)
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)), create=create
    )


def attributed_payload(quote="printf bad > config.ini"):
    return {
        "status": "ATTRIBUTED",
        "summary": "模型写入了错误配置。",
        "primary_cause": {
            "responsibility": "LLM",
            "category_code": "L3",
            "step_id": 2,
            "component": None,
            "contract_violation": "wrong_action",
            "claim": "模型写入错误配置。",
        },
        "secondary_factors": [],
        "failure_surface": {
            "step_id": None,
            "source": "verifier",
            "claim": "Verifier 失败。",
        },
        "evidence": [
            {
                "step_id": 2,
                "source": "agent",
                "quote": quote,
                "supports_claim": "该命令写入错误值。",
                "relation": "DIRECT_SUPPORT",
            }
        ],
        "causal_chain": [
            {"step_id": 2, "role": "TRIGGER", "claim": "写入错误值。"},
            {"step_id": None, "role": "FAILURE_SURFACE", "claim": "测试失败。"},
        ],
        "counterfactual": {
            "intervention": "写入正确配置。",
            "expected_effect": "测试可以通过。",
            "strength": "STRONG",
        },
        "rejected_candidates": [],
        "confidence": "HIGH",
    }


class PipelineTest(unittest.TestCase):
    def test_shipped_demo_runs_without_api_key(self):
        with tempfile.TemporaryDirectory() as temp:
            connection = connect(Path(temp) / "evalplant.db")
            demo = Path(__file__).resolve().parent.parent / "examples" / "demo-job"
            trajectory_id = import_run(connection, demo, "demo")[0]
            row = connection.execute(
                "SELECT * FROM trajectories WHERE id=?", (trajectory_id,)
            ).fetchone()
            result = analyze_trajectory(
                Path(row["raw_path"]), row["verdict"], row["health_status"]
            )
            self.assertEqual(result["matched_rule"], "missing_tool_result")
            connection.close()

    def test_database_has_five_tables(self):
        with tempfile.TemporaryDirectory() as temp:
            connection = connect(Path(temp) / "evalplant.db")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(
                tables,
                {"experiments", "trajectories", "steps", "diagnoses", "attempts"},
            )
            connection.close()

    def test_imports_harbor_atif(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw_path = harbor_run(root)
            connection = connect(root / "evalplant.db")
            trajectory_id = import_run(connection, root / "job", "smoke")[0]
            row = connection.execute(
                "SELECT * FROM trajectories WHERE id=?", (trajectory_id,)
            ).fetchone()
            self.assertEqual(row["verdict"], "FAIL")
            self.assertEqual(row["health_status"], "VALID")
            self.assertEqual(row["raw_path"], str(raw_path.resolve()))
            self.assertEqual(row["source_schema_version"], "ATIF-v1.7")
            self.assertEqual(row["canonical_schema_version"], "evalplant-canonical-v1")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM steps").fetchone()[0], 3
            )

            second_id = import_run(connection, root / "job", "smoke-2")[0]
            self.assertNotEqual(second_id, trajectory_id)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM trajectories").fetchone()[0],
                2,
            )
            connection.close()

    def test_missing_tool_result_is_harness_rule(self):
        with tempfile.TemporaryDirectory() as temp:
            raw_path = harbor_run(Path(temp), completed_tool=False)
            result = analyze_trajectory(raw_path, "FAIL", "VALID")
            self.assertEqual(result["responsibility"], "HARNESS")
            self.assertEqual(result["category_code"], "H-T")
            self.assertEqual(result["decision_source"], "RULE")

    def test_model_saying_truncated_is_not_a_harness_rule(self):
        with tempfile.TemporaryDirectory() as temp:
            raw_path = harbor_run(Path(temp))
            data = json.loads(raw_path.read_text(encoding="utf-8"))
            data["steps"][1]["message"] = "The file looks truncated, inspect it."
            raw_path.write_text(json.dumps(data), encoding="utf-8")
            client = mock_client({"status": "UNDETERMINED", "summary": "证据不足。"})
            result = analyze_trajectory(raw_path, "FAIL", "VALID", client=client)
            self.assertEqual(result["decision_source"], "LLM")
            client.create.assert_called_once()

    def test_normal_agent_timeout_is_left_for_llm(self):
        with tempfile.TemporaryDirectory() as temp:
            raw_path = harbor_run(Path(temp))
            result_path = raw_path.parent.parent / "result.json"
            data = json.loads(result_path.read_text(encoding="utf-8"))
            data["exception_info"] = {
                "exception_type": "AgentTimeoutError",
                "exception_message": "agent reached its normal time limit",
            }
            result_path.write_text(json.dumps(data), encoding="utf-8")
            client = mock_client({"status": "UNDETERMINED", "summary": "证据不足。"})
            result = analyze_trajectory(raw_path, "TIMEOUT", "VALID", client=client)
            self.assertEqual(result["decision_source"], "LLM")
            client.create.assert_called_once()

    def test_llm_is_called_once_and_evidence_is_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            raw_path = harbor_run(Path(temp))
            client = mock_client(attributed_payload())
            result = analyze_trajectory(raw_path, "FAIL", "VALID", client=client)
            self.assertEqual(result["category_code"], "L3")
            self.assertEqual(result["judge_input_tokens"], 50)
            self.assertEqual(result["prompt_version"], "engineering_diagnosis_v3")
            self.assertEqual(result["trajectory_mode"], "FULL")
            self.assertEqual(result["judge_call_count"], 1)
            self.assertEqual(result["causal_chain"][0]["role"], "TRIGGER")
            client.create.assert_called_once()

    def test_fake_evidence_fails_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            raw_path = harbor_run(Path(temp))
            client = mock_client(attributed_payload("not in the trace"))
            with self.assertRaisesRegex(ValueError, "not present"):
                analyze_trajectory(raw_path, "FAIL", "VALID", client=client)

    def test_unknown_atif_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            raw_path = harbor_run(Path(temp))
            data = json.loads(raw_path.read_text(encoding="utf-8"))
            data["schema_version"] = "ATIF-v9.9"
            raw_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                analyze_trajectory(raw_path, "FAIL", "VALID", client=mock_client({}))

    def test_structured_index_is_deterministic(self):
        index = build_structured_index(
            [
                {
                    "step_index": 7,
                    "role": "tool",
                    "action_type": "tool_error",
                    "content": "permission denied",
                    "command": None,
                    "tool_name": "shell",
                    "test_status": None,
                }
            ]
        )
        self.assertEqual(index[0]["event_type"], "tool_error")
        self.assertTrue(index[0]["has_explicit_error"])
        segments = build_segment_index(index)
        self.assertEqual(segments[0]["notable_step_ids"], [7])

    def test_judge_payload_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            raw_path = harbor_run(Path(temp))
            data = json.loads(raw_path.read_text(encoding="utf-8"))
            secret = "sk-" + "a" * 32
            data["steps"][1]["message"] = "Use %s" % secret
            raw_path.write_text(json.dumps(data), encoding="utf-8")
            client = mock_client({"status": "UNDETERMINED", "reason": "证据不足。"})
            analyze_trajectory(raw_path, "FAIL", "VALID", client=client)
            request = client.create.call_args.kwargs["messages"][1]["content"]
            self.assertNotIn(secret, request)
            self.assertIn("<REDACTED_SECRET>", request)

    def test_long_trace_can_request_one_evidence_expansion(self):
        with tempfile.TemporaryDirectory() as temp:
            raw_path = harbor_run(Path(temp))
            data = json.loads(raw_path.read_text(encoding="utf-8"))
            data["steps"][1]["message"] = "x" * 30000
            raw_path.write_text(json.dumps(data), encoding="utf-8")
            client = mock_client(
                [
                    {
                        "status": "NEED_MORE_EVIDENCE",
                        "reason": "需要查看第 2 步。",
                        "requested_step_ids": [2],
                    },
                    {"status": "UNDETERMINED", "reason": "仍然证据不足。"},
                ]
            )
            result = analyze_trajectory(
                raw_path, "FAIL", "VALID", max_input_tokens=7000, client=client
            )
            self.assertEqual(result["status"], "UNDETERMINED")
            self.assertEqual(result["trajectory_mode"], "HIERARCHICAL")
            self.assertEqual(result["judge_call_count"], 2)
            self.assertEqual(client.create.call_count, 2)

    def test_very_long_trace_uses_bounded_local_index(self):
        with tempfile.TemporaryDirectory() as temp:
            raw_path = harbor_run(Path(temp))
            data = json.loads(raw_path.read_text(encoding="utf-8"))
            data["steps"] = [
                {
                    "step_id": step_id,
                    "source": "agent",
                    "message": "ordinary progress " + "x" * 80,
                }
                for step_id in range(4000)
            ]
            raw_path.write_text(json.dumps(data), encoding="utf-8")
            client = mock_client({"status": "UNDETERMINED", "reason": "证据不足。"})
            result = analyze_trajectory(raw_path, "FAIL", "VALID", client=client)
            self.assertEqual(result["trajectory_mode"], "HIERARCHICAL")
            self.assertEqual(result["judge_call_count"], 1)
            client.create.assert_called_once()

    def test_oversized_trace_is_not_sent(self):
        with tempfile.TemporaryDirectory() as temp:
            raw_path = harbor_run(Path(temp))
            client = mock_client({})
            result = analyze_trajectory(
                raw_path, "FAIL", "VALID", max_input_tokens=1, client=client
            )
            self.assertEqual(result["status"], "INPUT_TOO_LARGE")
            client.create.assert_not_called()

    def test_report_counts_diagnosis(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw_path = harbor_run(root)
            connection = connect(root / "evalplant.db")
            trajectory_id = import_run(connection, root / "job", "smoke")[0]
            diagnosis = analyze_trajectory(
                raw_path, "FAIL", "VALID", max_input_tokens=1
            )
            save_diagnosis(connection, trajectory_id, diagnosis)
            result = report(connection, "smoke")
            self.assertEqual(result["failed_tasks"], 1)
            self.assertEqual(result["diagnosis_statuses"], {"INPUT_TOO_LARGE": 1})
            self.assertEqual(result["average_input_tokens"], 100.0)
            output = root / "report.json"
            command_report(
                SimpleNamespace(experiment="smoke", output=str(output)), connection
            )
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exported["statistics"]["failed_tasks"], 1)
            self.assertEqual(len(exported["diagnoses"]), 1)
            connection.close()

    def test_token_estimate_is_conservative(self):
        self.assertEqual(estimate_tokens("abcd"), 2)

    def test_evaluation_reports_accuracy_and_stability(self):
        gold = [
            {
                "case_id": "a",
                "responsibility": "LLM",
                "category_code": "L3",
                "root_cause_step": 2,
            }
        ]
        prediction = {
            "a": {
                "status": "ATTRIBUTED",
                "responsibility": "LLM",
                "category_code": "L3",
                "root_cause_step": 3,
            }
        }
        result = evaluate(gold, prediction)
        self.assertEqual(result["responsibility_accuracy"], 1.0)
        self.assertEqual(result["root_step_exact_accuracy"], 0.0)
        self.assertEqual(result["root_step_near_accuracy"], 1.0)
        self.assertEqual(result["selective_category_accuracy"], 1.0)
        self.assertIsNone(result["evidence_support_rate"])
        self.assertEqual(stability([prediction, prediction])["exact_agreement"], 1.0)

    def test_harbor_events_track_retries_and_lost_workers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = [
                {
                    "event_version": 1,
                    "job_id": "j",
                    "trial_id": "a1",
                    "trial_name": "task",
                    "task_name": "task",
                    "event": "start",
                    "state": "RUNNING",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                },
                {
                    "event_version": 1,
                    "job_id": "j",
                    "trial_id": "a1",
                    "trial_name": "task",
                    "task_name": "task",
                    "event": "end",
                    "state": "INFRA_ERROR",
                    "timestamp": "2026-01-01T00:01:00+00:00",
                    "retryable": True,
                    "exception_type": "EnvironmentStartTimeoutError",
                },
                {
                    "event_version": 1,
                    "job_id": "j",
                    "trial_id": "a2",
                    "trial_name": "task",
                    "task_name": "task",
                    "event": "start",
                    "state": "RUNNING",
                    "timestamp": "2026-01-01T00:02:00+00:00",
                },
            ]
            (root / "execution-events.jsonl").write_text(
                "\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8"
            )
            connection = connect(root / "evalplant.db")
            self.assertEqual(sync_execution_events(connection, root, "run"), 2)
            result = execution_status(connection, "run", lost_after_seconds=1)
            self.assertEqual(result["retries"], 1)
            self.assertEqual(result["states"], {"LOST": 1})
            sync_execution_events(connection, root, "run")
            sync_execution_events(connection, root, "run-copy")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 4
            )
            connection.close()

    def test_cli_exposes_delivery_commands(self):
        choices = next(
            action.choices
            for action in parser()._actions
            if getattr(action, "choices", None)
        )
        self.assertEqual(
            set(choices), {"import", "inspect", "observe", "analyze", "report"}
        )


if __name__ == "__main__":
    unittest.main()
