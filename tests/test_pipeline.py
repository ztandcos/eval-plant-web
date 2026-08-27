import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rich.console import Console

from evalplant import cli
from evalplant.cli import command_inspect, command_report, main, parser, run_pipeline
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
from evalplant.metrics import compare_experiments, report


def harbor_trial(
    job: Path,
    trial_name: str,
    task_name: str,
    *,
    completed_tool=True,
    reward=0.0,
    trajectory_id=None,
    agent_name="dsh",
) -> Path:
    trial = job / trial_name
    agent = trial / "agent"
    verifier = trial / "verifier"
    agent.mkdir(parents=True, exist_ok=True)
    verifier.mkdir(exist_ok=True)
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
                    "name": agent_name,
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
                "id": trajectory_id or trial_name,
                "task_name": task_name,
                "trial_name": trial_name,
                "agent_info": {
                    "name": agent_name,
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


def harbor_run(root: Path, *, completed_tool=True, reward=0.0) -> Path:
    return harbor_trial(
        root / "job",
        "task__trial",
        "terminal-bench/task",
        completed_tool=completed_tool,
        reward=reward,
        trajectory_id="trajectory-1",
    )


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

    def test_harbor_result_without_trajectory_is_undetermined(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trial = root / "job" / "hello__trial"
            (trial / "agent").mkdir(parents=True)
            (trial / "result.json").write_text(
                json.dumps(
                    {
                        "id": "outcome-only-1",
                        "task_name": "demo/hello",
                        "trial_name": "hello__trial",
                        "agent_info": {"name": "oracle", "version": "1.0.0"},
                        "agent_result": {},
                        "verifier_result": {"rewards": {"reward": 0.0}},
                    }
                ),
                encoding="utf-8",
            )
            connection = connect(root / "evalplant.db")
            trajectory_id = import_run(connection, root / "job", "outcome-only")[0]
            row = connection.execute(
                "SELECT verdict, source_schema_version FROM trajectories WHERE id=?",
                (trajectory_id,),
            ).fetchone()
            self.assertEqual(
                dict(row),
                {
                    "verdict": "FAIL",
                    "source_schema_version": "harbor-result-v1",
                },
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM steps WHERE trajectory_id=?",
                    (trajectory_id,),
                ).fetchone()[0],
                0,
            )
            output = root / "outcome-only-report.json"
            payload = run_pipeline(
                connection,
                root / "job",
                "outcome-only",
                "not-called",
                once=True,
                output=output,
            )
            diagnosis = payload["diagnoses"][0]["diagnosis"]
            self.assertEqual(diagnosis["status"], "UNDETERMINED")
            self.assertEqual(
                diagnosis["matched_rule"], "trajectory_unavailable"
            )
            connection.close()

    def test_database_has_evaluation_tables(self):
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
                {
                    "experiments",
                    "tasks",
                    "attempts",
                    "trajectories",
                    "outcomes",
                    "checks",
                    "steps",
                    "diagnoses",
                },
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
            outcome = connection.execute("SELECT * FROM outcomes").fetchone()
            check = connection.execute("SELECT * FROM checks").fetchone()
            self.assertEqual(outcome["task_key"], "terminal-bench/task")
            self.assertEqual(outcome["status"], "FAIL")
            self.assertEqual(check["name"], "reward:task")
            self.assertEqual(check["status"], "FAIL")

            second_id = import_run(connection, root / "job", "smoke-2")[0]
            self.assertNotEqual(second_id, trajectory_id)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM trajectories").fetchone()[0],
                2,
            )
            connection.close()
            connection = connect(root / "evalplant.db")
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM checks WHERE trajectory_id=?",
                    (trajectory_id,),
                ).fetchone()[0],
                1,
            )
            connection.close()

    def test_explicit_verifier_checks_are_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw_path = harbor_run(root)
            result_path = raw_path.parent.parent / "result.json"
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            payload["verifier_result"] = {
                "checks": [
                    {
                        "name": "file-created",
                        "kind": "CODE",
                        "status": "PASS",
                        "evidence": "workspace/config.ini exists",
                    }
                ]
            }
            result_path.write_text(json.dumps(payload), encoding="utf-8")
            connection = connect(root / "evalplant.db")
            trajectory_id = import_run(connection, root / "job", "checks")[0]
            outcome = connection.execute(
                "SELECT status FROM outcomes WHERE trajectory_id=?", (trajectory_id,)
            ).fetchone()
            check = connection.execute(
                "SELECT * FROM checks WHERE trajectory_id=?", (trajectory_id,)
            ).fetchone()
            self.assertEqual(outcome["status"], "PASS")
            self.assertEqual(check["name"], "file-created")
            self.assertEqual(check["status"], "PASS")
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
            self.assertEqual(result["total_trials"], 1)
            self.assertEqual(result["weighted_check_pass_rate"], 0.0)
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
            set(choices),
            {
                "bench",
                "run",
                "diagnose",
                "import",
                "inspect",
                "observe",
                "analyze",
                "report",
                "compare",
            },
        )

    def test_compare_multiple_trials_and_ship_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for experiment, outcomes in (
                ("baseline", {"a": [1.0, 0.0], "b": [0.0, 0.0]}),
                ("candidate", {"a": [1.0, 1.0], "b": [1.0, 0.0]}),
            ):
                job = root / experiment
                for task, rewards in outcomes.items():
                    for trial, reward in enumerate(rewards, 1):
                        harbor_trial(
                            job,
                            "%s-%s" % (task, trial),
                            "terminal-bench/%s" % task,
                            reward=reward,
                            trajectory_id="%s-%s-%s" % (experiment, task, trial),
                        )
                connection = connect(root / "evalplant.db")
                import_run(connection, job, experiment)
                connection.close()
            connection = connect(root / "evalplant.db")
            result = compare_experiments(connection, "baseline", "candidate", k=2)
            self.assertEqual(result["eligible_tasks"], 2)
            self.assertEqual(result["baseline_metrics"]["pass_at_k"], 0.5)
            self.assertEqual(result["candidate_metrics"]["pass_at_k"], 1.0)
            self.assertEqual(result["candidate_metrics"]["pass_power_k"], 0.5)
            self.assertEqual(result["changes"]["improved"], 1)
            self.assertEqual(result["changes"]["regressed"], 0)
            self.assertEqual(result["ship_gate"]["status"], "PASS")
            regression = compare_experiments(connection, "candidate", "baseline", k=2)
            self.assertEqual(regression["changes"]["regressed"], 1)
            self.assertEqual(regression["ship_gate"]["status"], "FAIL")
            stats = report(connection, "candidate")
            self.assertEqual(stats["total_tasks"], 2)
            self.assertEqual(stats["total_trials"], 4)
            self.assertEqual(stats["trial_pass_rate"], 0.75)
            connection.close()
            output = root / "comparison.json"
            self.assertEqual(
                main(
                    [
                        "--db",
                        str(root / "evalplant.db"),
                        "compare",
                        "--baseline",
                        "baseline",
                        "--candidate",
                        "candidate",
                        "--k",
                        "2",
                        "--output",
                        str(output),
                    ]
                ),
                0,
            )
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["ship_gate"]["status"],
                "PASS",
            )

    def test_run_demo_oneshots_without_api_key(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "evalplant.db"
            report_path = root / "demo-report.json"
            demo = Path(__file__).resolve().parent.parent / "examples" / "demo-job"
            code = main(
                [
                    "--db",
                    str(db),
                    "run",
                    str(demo),
                    "--experiment",
                    "demo",
                    "--model",
                    "not-called",
                    "--output",
                    str(report_path),
                ]
            )
            self.assertEqual(code, 0)
            connection = connect(db)
            row = connection.execute("SELECT * FROM trajectories").fetchone()
            diagnosis = connection.execute("SELECT * FROM diagnoses").fetchone()
            self.assertEqual(row["agent_name"], "dsh-minimal")
            self.assertEqual(row["source_dataset"], "demo")
            self.assertEqual(row["source_instance_id"], "tool-result-missing")
            self.assertEqual(diagnosis["matched_rule"], "missing_tool_result")
            exported = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exported["statistics"]["failed_tasks"], 1)
            self.assertEqual(exported["diagnoses"][0]["agent"], "dsh-minimal")
            self.assertEqual(exported["diagnoses"][0]["dataset"], "demo")
            buffer = StringIO()
            with patch.object(
                cli, "console", Console(file=buffer, force_terminal=False)
            ):
                command_inspect(SimpleNamespace(trajectory=row["id"]), connection)
            text = buffer.getvalue()
            self.assertIn("Agent: dsh-minimal", text)
            self.assertIn("Dataset: demo", text)
            self.assertIn("Task: tool-result-missing", text)
            connection.close()

    def test_agent_and_bench_are_independent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harbor_trial(
                root / "job",
                "openhands__demo-task",
                "swe-bench/demo-task",
                completed_tool=False,
                agent_name="OpenHands",
                trajectory_id="openhands-1",
            )
            connection = connect(root / "evalplant.db")
            trajectory_id = import_run(connection, root / "job", "decouple")[0]
            row = connection.execute(
                "SELECT * FROM trajectories WHERE id=?", (trajectory_id,)
            ).fetchone()
            self.assertEqual(row["agent_name"], "OpenHands")
            self.assertEqual(row["source_dataset"], "swe-bench")
            self.assertEqual(row["source_instance_id"], "demo-task")
            self.assertEqual(row["base_task_id"], "demo-task")
            self.assertNotIn("OpenHands", row["base_task_id"])
            stats = report(connection, "decouple")
            self.assertIn("OpenHands", stats["by_agent"])
            self.assertIn("swe-bench", stats["by_dataset"])
            connection.close()

    def test_run_watches_live_job_and_diagnoses_new_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job = root / "job"
            job.mkdir()
            harbor_trial(
                job,
                "pass__trial",
                "terminal-bench/ok-task",
                reward=1.0,
                trajectory_id="pass-1",
            )
            now = datetime.now(timezone.utc)

            def stamp(seconds):
                return (now - timedelta(seconds=seconds)).isoformat()

            def event(trial_id, trial_name, task_name, name, state, seconds):
                return {
                    "event_version": 1,
                    "job_id": "live",
                    "trial_id": trial_id,
                    "trial_name": trial_name,
                    "task_name": task_name,
                    "event": name,
                    "state": state,
                    "timestamp": stamp(seconds),
                }

            events = [
                event(
                    "p1",
                    "pass__trial",
                    "terminal-bench/ok-task",
                    "start",
                    "RUNNING",
                    30,
                ),
                event(
                    "p1",
                    "pass__trial",
                    "terminal-bench/ok-task",
                    "end",
                    "SUCCEEDED",
                    20,
                ),
                event(
                    "f1",
                    "fail__trial",
                    "terminal-bench/fail-task",
                    "start",
                    "RUNNING",
                    10,
                ),
            ]
            events_path = job / "execution-events.jsonl"
            events_path.write_text(
                "\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8"
            )

            def inject(tick):
                if tick != 1:
                    return
                harbor_trial(
                    job,
                    "fail__trial",
                    "terminal-bench/fail-task",
                    completed_tool=False,
                    reward=0.0,
                    trajectory_id="fail-1",
                )
                events.append(
                    event(
                        "f1",
                        "fail__trial",
                        "terminal-bench/fail-task",
                        "end",
                        "FAILED",
                        0,
                    )
                )
                events_path.write_text(
                    "\n".join(json.dumps(item) for item in events) + "\n",
                    encoding="utf-8",
                )

            connection = connect(root / "evalplant.db")
            output = root / "live-report.json"
            buffer = StringIO()
            with patch.object(
                cli, "console", Console(file=buffer, force_terminal=False)
            ):
                payload = run_pipeline(
                    connection,
                    job,
                    "live",
                    "not-called",
                    once=False,
                    output=output,
                    poll_seconds=0,
                    lost_after_seconds=90,
                    inject=inject,
                    max_polls=5,
                )
            text = buffer.getvalue()
            self.assertIn("OK", text)
            self.assertIn("terminal-bench/ok-task", text)
            self.assertIn("FAIL", text)
            self.assertIn("terminal-bench/fail-task", text)
            self.assertIn("ATTRIBUTED", text)
            self.assertIn(str(output), text)
            self.assertEqual(payload["statistics"]["successful_tasks"], 1)
            self.assertEqual(payload["statistics"]["failed_tasks"], 1)
            self.assertEqual(len(payload["diagnoses"]), 1)
            self.assertEqual(payload["diagnoses"][0]["instance_id"], "fail-task")
            diagnoses = connection.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[
                0
            ]
            self.assertEqual(diagnoses, 1)
            connection.close()


if __name__ == "__main__":
    unittest.main()
