"""Official DeepSeek Harness minimal SDK integration."""

import asyncio
import shlex
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, override

from packaging.version import InvalidVersion, Version

from harbor.agents.installed.base import (
    AgentAuthenticationError,
    BaseInstalledAgent,
    ErrorPattern,
    ModelNotFoundError,
    NonZeroAgentExitCodeError,
    with_prompt_template,
)
from harbor.agents.model_connection import ModelConnectionSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.agent.name import AgentName
from harbor.models.trajectories import Trajectory
from harbor.models.trial.paths import EnvironmentPaths


class DshMinimal(BaseInstalledAgent):
    """Run the official two-tool DeepSeek Harness SDK composition."""

    SUPPORTS_ATIF = True
    SUPPORTS_CONFIG = False
    SUPPORTS_RESUME = False

    MODEL_CONNECTION = ModelConnectionSpec(
        api_key_envs=("DEEPSEEK_API_KEY",),
        base_url_envs=("DEEPSEEK_BASE_URL",),
        passthrough=True,
    )
    ERROR_PATTERNS = BaseInstalledAgent.ERROR_PATTERNS + [
        ErrorPattern(r"MISSING_CREDENTIAL", AgentAuthenticationError),
        ErrorPattern(r"UNKNOWN_MODEL", ModelNotFoundError),
    ]

    _PACKAGE = "deepseek-harness-sdk"
    _DEFAULT_VERSION = "0.1.0rc7"
    _APT_INSTALL_ATTEMPTS = 3
    _APT_INSTALL_RETRY_DELAY_SEC = 5
    _PYTHON_VERSION = "3.12"
    _VENV_DIR = PurePosixPath("/opt/harbor-dsh-minimal-venv")
    _PYTHON = _VENV_DIR / "bin/python"
    _REMOTE_RUNNER = PurePosixPath("/installed-agent/dsh_minimal_runner.py")
    _REMOTE_CONFIG = PurePosixPath("/installed-agent/dsh_minimal.cordis.yml")
    _REMOTE_ENV = PurePosixPath("/installed-agent/dsh_minimal.env")
    _OUTPUT_FILENAME = "dsh-minimal.txt"
    _TRAJECTORY_FILENAME = "trajectory.json"
    _PLACEHOLDER_API_KEY = "not-required"

    def __init__(
        self, *args: Any, python_version: str = _PYTHON_VERSION, **kwargs: Any
    ) -> None:
        kwargs.setdefault("version", self._DEFAULT_VERSION)
        super().__init__(*args, **kwargs)
        self._python_version = str(python_version)
        if self.skills_dir or self.mcp_servers:
            raise ValueError("dsh-minimal does not support Skills or MCP servers")

    @staticmethod
    @override
    def name() -> str:
        return AgentName.DSH_MINIMAL.value

    @override
    def get_version_command(self) -> str:
        return (
            f'{self._PYTHON} -c "from importlib.metadata import version; '
            f"print(version('{self._PACKAGE}'))\""
        )

    async def _sdk_available(self, environment: BaseEnvironment) -> bool:
        result = await environment.exec(command=self.get_version_command())
        if result.return_code != 0:
            return False
        if self._version is None:
            return True
        try:
            return Version((result.stdout or "").strip()) == Version(self._version)
        except InvalidVersion:
            return False

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        installed = await self._sdk_available(environment)
        await self._configure_apt(environment)
        # ca_certificates is always_install=True in BaseInstalledAgent, which
        # forces apt-get update on every trial. Skip it when the bundle exists
        # so flaky Ubuntu mirrors cannot fail otherwise-ready images.
        certs = await environment.exec(
            command=(
                "test -f /etc/ssl/certs/ca-certificates.crt "
                "|| test -f /etc/ssl/cert.pem"
            ),
            user="root",
        )
        wanted = ("bash", "coreutils") if installed else ("curl", "bash", "coreutils")
        if certs.return_code != 0:
            wanted = wanted + ("ca_certificates",)
        dependencies = await self._missing_system_dependencies(environment, wanted)
        if dependencies:
            await self.ensure_system_dependencies(environment, dependencies)

        if not installed:
            agent_user = shlex.quote(str(environment.default_user or "root"))
            venv = shlex.quote(self._VENV_DIR.as_posix())
            await self.exec_as_root(
                environment,
                command=f"mkdir -p {venv} && chown -R {agent_user}:{agent_user} {venv}",
            )
            version_spec = f"=={self._version}" if self._version else ""
            package = shlex.quote(f"{self._PACKAGE}{version_spec}")
            await self.exec_as_agent(
                environment,
                command=(
                    "set -euo pipefail; "
                    "curl -LsSf https://astral.sh/uv/install.sh | sh && "
                    'if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; fi && '
                    f"uv python install {shlex.quote(self._python_version)} && "
                    f"uv venv {venv} --python {shlex.quote(self._python_version)} --clear && "
                    f"uv pip install --python {self._PYTHON} {package} && "
                    f"{self.get_version_command()}"
                ),
            )

        await self._upload_asset(
            environment, "dsh_minimal_runner.py", self._REMOTE_RUNNER
        )
        await self._upload_asset(
            environment, "dsh_minimal.cordis.yml", self._REMOTE_CONFIG
        )
        await self.exec_as_root(
            environment,
            command=f"chmod a+rX {self._REMOTE_RUNNER} {self._REMOTE_CONFIG}",
        )

    @override
    async def _missing_system_dependencies(
        self, environment: BaseEnvironment, dependencies: tuple[str, ...]
    ) -> tuple[str, ...]:
        missing: list[str] = []
        for name in dependencies:
            spec = self.SYSTEM_PACKAGES[name]
            if spec.always_install or not spec.commands:
                missing.append(name)
                continue
            check = " && ".join(
                f"command -v {shlex.quote(command)} >/dev/null 2>&1"
                for command in spec.commands
            )
            result = await environment.exec(command=check, user="root")
            if result.return_code != 0:
                missing.append(name)
        return tuple(missing)

    async def ensure_system_dependencies(
        self, environment: BaseEnvironment, dependencies: tuple[str, ...]
    ) -> None:
        for attempt in range(self._APT_INSTALL_ATTEMPTS):
            try:
                await super().ensure_system_dependencies(environment, dependencies)
                return
            except NonZeroAgentExitCodeError as exc:
                text = str(exc)
                transient = (
                    "Mirror sync in progress" in text
                    or "File has unexpected size" in text
                    or "apt-get update" in text
                    or " 500 " in text
                )
                if not transient or attempt == self._APT_INSTALL_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(self._APT_INSTALL_RETRY_DELAY_SEC)

    async def _configure_apt(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(
            environment,
            command=(
                "mkdir -p /etc/apt/apt.conf.d && "
                'printf \'Acquire::By-Hash "false";\\n'
                'Acquire::Retries "5";\\n'
                'Acquire::http::Timeout "30";\\n'
                'Acquire::https::Timeout "30";\\n\' '
                "> /etc/apt/apt.conf.d/99harbor-apt"
            ),
        )

    async def _upload_asset(
        self, environment: BaseEnvironment, filename: str, remote: PurePosixPath
    ) -> None:
        source = Path(__file__).with_name(filename)
        local_copy = self.logs_dir / filename
        local_copy.write_text(source.read_text())
        await environment.upload_file(local_copy, remote.as_posix())

    def _resolve_model(self) -> tuple[str, str | None]:
        if not self.model_name:
            raise ValueError("Model name is required")
        connection = self.model_connection
        if connection.provider != "deepseek":
            raise ValueError("dsh-minimal supports DeepSeek models only")
        if not connection.api_key and not connection.configured_base_url:
            raise ValueError(
                "Set DEEPSEEK_API_KEY, or DEEPSEEK_BASE_URL for a keyless endpoint"
            )
        model = self.model_name.split("/", 1)[-1]
        if not model:
            raise ValueError(f"Invalid model name: {self.model_name!r}")
        return model, connection.configured_base_url

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        model, base_url = self._resolve_model()
        connection = self.model_connection
        env = {
            **connection.env,
            "DEEPSEEK_API_KEY": connection.api_key or self._PLACEHOLDER_API_KEY,
            "DSH_MODEL": model,
            "DSH_AGENT_VERSION": self._version or "unknown",
            "DSH_SESSION_ROOT": (
                EnvironmentPaths.agent_dir / "dsh-sessions"
            ).as_posix(),
            "DSH_TRAJECTORY_PATH": (
                EnvironmentPaths.agent_dir / self._TRAJECTORY_FILENAME
            ).as_posix(),
        }
        if base_url:
            env["DEEPSEEK_BASE_URL"] = base_url

        local_env: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", delete=False) as env_file:
                local_env = Path(env_file.name)
                for key, value in env.items():
                    env_file.write(f"export {key}={shlex.quote(str(value))}\n")
            local_env.chmod(0o600)
            await environment.upload_file(local_env, self._REMOTE_ENV.as_posix())
        finally:
            if local_env is not None:
                local_env.unlink(missing_ok=True)

        agent_user = shlex.quote(str(environment.default_user or "root"))
        remote_env = shlex.quote(self._REMOTE_ENV.as_posix())
        await self.exec_as_root(
            environment,
            command=f"chown {agent_user}:{agent_user} {remote_env} && chmod 600 {remote_env}",
        )
        log_path = (EnvironmentPaths.agent_dir / self._OUTPUT_FILENAME).as_posix()
        await self.exec_as_agent(
            environment,
            command=(
                f"set -euo pipefail; trap 'rm -f {remote_env}' EXIT; "
                f". {remote_env}; "
                f"{self._PYTHON} {self._REMOTE_RUNNER} "
                f"--cordis {self._REMOTE_CONFIG} {shlex.quote(instruction)} "
                f"2>&1 | stdbuf -oL tee {shlex.quote(log_path)}"
            ),
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        trajectory_path = self.logs_dir / self._TRAJECTORY_FILENAME
        if not trajectory_path.exists():
            self.logger.debug("No dsh-minimal trajectory found")
            return
        try:
            trajectory = Trajectory.model_validate_json(trajectory_path.read_text())
        except (OSError, ValueError) as exc:
            self.logger.debug(f"Failed to read dsh-minimal trajectory: {exc}")
            return
        if trajectory.final_metrics:
            context.cost_usd = trajectory.final_metrics.total_cost_usd
            context.n_input_tokens = trajectory.final_metrics.total_prompt_tokens or 0
            context.n_output_tokens = (
                trajectory.final_metrics.total_completion_tokens or 0
            )
            context.n_cache_tokens = trajectory.final_metrics.total_cached_tokens or 0
