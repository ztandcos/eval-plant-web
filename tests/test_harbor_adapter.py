import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evalplant import cli
from evalplant.cli import command_bench, main
from evalplant.db import connect
from evalplant.harbor_adapter import (
    build_job_config,
    find_harbor_binary,
    job_dir_from_config,
    resolve_agent,
    resolve_bench,
)
from test_pipeline import harbor_trial


class FakeProcess:
    def __init__(self, code=0):
        self.returncode = code

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15


class HarborAdapterTest(unittest.TestCase):
    def test_agent_aliases_do_not_expose_harbor_cli(self):
        agent = resolve_agent("dsh")
        self.assertEqual(agent["name"], "dsh-minimal")
        self.assertEqual(agent["kwargs"]["version"], "0.1.0rc7")
        self.assertEqual(resolve_agent("claude-code")["name"], "claude-code")
        self.assertEqual(resolve_agent("codex", "gpt-5")["model_name"], "gpt-5")

    def test_local_bench_uses_path_and_registry_uses_name(self):
        with tempfile.TemporaryDirectory() as temp:
            tasks = Path(temp) / "tasks"
            tasks.mkdir()
            local = resolve_bench(str(tasks), ["hello-world"])
            self.assertEqual(local["path"], str(tasks.resolve()))
            self.assertEqual(local["task_names"], ["hello-world"])
        remote = resolve_bench("terminal-bench")
        self.assertEqual(remote, {"name": "terminal-bench"})

    def test_job_config_keeps_secrets_as_templates(self):
        with tempfile.TemporaryDirectory() as temp:
            jobs = Path(temp) / "jobs"
            tasks = Path(temp) / "tasks"
            tasks.mkdir()
            previous = os.environ.get("DEEPSEEK_API_KEY")
            os.environ["DEEPSEEK_API_KEY"] = "DO_NOT_LEAK_THIS_VALUE"
            try:
                config = build_job_config(
                    agents=["dsh", "codex"],
                    benches=[str(tasks), "swe-bench"],
                    sandbox="docker",
                    k=3,
                    concurrency=2,
                    job_name="matrix",
                    jobs_dir=jobs,
                    model="deepseek/deepseek-v4-flash",
                    tasks=["hello-world"],
                    env_names=["DEEPSEEK_API_KEY"],
                )
            finally:
                if previous is None:
                    del os.environ["DEEPSEEK_API_KEY"]
                else:
                    os.environ["DEEPSEEK_API_KEY"] = previous
            dumped = json.dumps(config)
            self.assertNotIn("DO_NOT_LEAK_THIS_VALUE", dumped)
            self.assertEqual(
                config["environment"]["env"]["DEEPSEEK_API_KEY"],
                "${DEEPSEEK_API_KEY}",
            )
            self.assertEqual(config["environment"]["type"], "docker")
            self.assertFalse(config["quiet"])
            self.assertEqual(config["n_attempts"], 3)
            self.assertEqual(len(config["agents"]), 2)
            self.assertEqual(config["agents"][1]["name"], "codex")
            self.assertEqual(config["datasets"][0]["path"], str(tasks.resolve()))
            self.assertEqual(config["datasets"][1]["name"], "swe-bench")
            self.assertEqual(job_dir_from_config(config), jobs.resolve() / "matrix")

    def test_unknown_sandbox_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                build_job_config(
                    agents=["dsh"],
                    benches=["terminal-bench"],
                    sandbox="not-a-sandbox",
                    job_name="x",
                    jobs_dir=Path(temp),
                )

    def test_find_harbor_uses_env_override(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "harbor"
            binary.write_text("", encoding="utf-8")
            binary.chmod(0o755)
            with patch.dict(os.environ, {"EVALPLANT_HARBOR": str(binary)}):
                self.assertEqual(find_harbor_binary(), binary)

    def test_find_harbor_prefers_patched_local_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / "local-harbor"
            local.write_text("", encoding="utf-8")
            upstream = Path(temp) / "upstream-harbor"
            upstream.write_text("", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                with patch("evalplant.harbor_adapter.LOCAL_HARBOR_BINARY", local):
                    with patch(
                        "evalplant.harbor_adapter.shutil.which",
                        return_value=str(upstream),
                    ):
                        self.assertEqual(find_harbor_binary(), local)

    def test_print_config_does_not_launch_harbor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tasks = root / "tasks"
            tasks.mkdir()
            db = root / "evalplant.db"
            launched = []

            def fake_launch(config_path, binary=None, cwd=None):
                launched.append(config_path)
                return FakeProcess()

            with patch.object(cli, "launch_harbor", fake_launch):
                code = main(
                    [
                        "--db",
                        str(db),
                        "bench",
                        "--agent",
                        "dsh",
                        "--bench",
                        str(tasks),
                        "--print-config",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(launched, [])
            written = list((root / "jobs").glob("*.evalplant.json"))
            self.assertEqual(len(written), 1)
            payload = json.loads(written[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["agents"][0]["name"], "dsh-minimal")

    def test_bench_launches_harbor_then_diagnoses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tasks = root / "tasks"
            tasks.mkdir()
            db = root / "evalplant.db"

            def fake_launch(config_path, binary=None, cwd=None):
                config = json.loads(Path(config_path).read_text(encoding="utf-8"))
                job = job_dir_from_config(config)
                harbor_trial(
                    job,
                    "dsh__fail",
                    "demo/fail-task",
                    completed_tool=False,
                    agent_name="dsh-minimal",
                    trajectory_id="bench-1",
                )
                return FakeProcess(0)

            with patch.object(
                cli, "find_harbor_binary", lambda: Path("/tmp/fake-harbor")
            ):
                with patch.object(cli, "launch_harbor", fake_launch):
                    connection = connect(db)
                    command_bench(
                        SimpleNamespace(
                            list=False,
                            print_config=False,
                            agents=["dsh"],
                            benches=[str(tasks)],
                            tasks=None,
                            sandbox="docker",
                            k=1,
                            concurrency=2,
                            experiment="one-shot",
                            jobs_dir=str(root / "jobs"),
                            agent_model=None,
                            agent_kwarg=None,
                            env=None,
                            force_build=False,
                            harbor=None,
                            model="not-called",
                            max_input_tokens=4096,
                            force=False,
                            gold=None,
                            output=str(root / "report.json"),
                            poll_seconds=0.0,
                            lost_after_seconds=90,
                        ),
                        connection,
                    )
                    diagnoses = connection.execute(
                        "SELECT COUNT(*) FROM diagnoses"
                    ).fetchone()[0]
                    self.assertEqual(diagnoses, 1)
                    connection.close()
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["statistics"]["failed_tasks"], 1)

    def test_list_and_missing_agent_are_evalplant_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            db = str(Path(temp) / "evalplant.db")
            self.assertEqual(main(["--db", db, "bench", "--list"]), 0)
            self.assertEqual(
                main(["--db", db, "bench", "--bench", "terminal-bench"]),
                1,
            )


if __name__ == "__main__":
    unittest.main()
