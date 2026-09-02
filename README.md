# EvalPlat

一份 YAML、一条 `evalplat eval`、一次 Harbor Job。所有 Agent、任务和 Trial 在同一个 Job 里并行；每条最终非 PASS 的 Trial 都会诊断；有 `comparison` 时再打回归标签并计算发布 Gate。

```text
YAML → evalplat eval → 一个 Harbor Job
     → 最终 Trial Outcome
     → 所有最终非 PASS 诊断
     → 可选 Agent 比较 / Gate
```

真实 TerminalBench 题目不会被测试夹具改写。六种 Outcome 用本地 Mock Harbor 返回物验证状态机。`evalplant` 是同一 CLI 的别名。

## 配置

导出 provider 的标准密钥后，直接执行命令即可。EvalPlat 按模型前缀和 agent 自动完成鉴权与协议适配；密钥只进入运行它的 agent，不写入 YAML、Job 配置或任务容器。

```bash
export DEEPSEEK_API_KEY='你的 DeepSeek Key'
uv run evalplat bench \
  --agent codex --agent claude-code \
  --agent-model deepseek/deepseek-v4-flash \
  --bench evals --task fix-off-by-one --concurrency 2
```

上面的命令会在一个 Harbor Job 中并发执行 Codex 和 Claude Code。单 agent 只保留一个 `--agent codex`；任意本地 bench 或 Harbor dataset 都可替换 `--bench`，需要限制题目时加 `--task`。目前 DeepSeek 的 Codex Responses API 与 Claude Code Anthropic API 会自动适配；其他 provider 则导出它们惯用的标准密钥，例如 `OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY`。

YAML 只支持 `agents`（至少一个）。每个 Agent 可写 `name / agent / model / n_concurrent / agent_kwargs / agent_env`。`agent_env` 仅用于特殊网关或非标准环境变量；正常 provider 不需要写。可选嵌套 `diagnosis` 和 `recovery`。可选 `comparison`；没有它时就是绝对评测，不生成回归标签。

本机的 Codex/Claude Code 稳定配置是 [suites/deepseek-codex-claude-parallel.yaml](suites/deepseek-codex-claude-parallel.yaml)。它会先构建并缓存一次共享 Agent 镜像；镜像构建、镜像内 apt/npm 安装和 Harbor 基础设施错误都有重试。直接用一条命令运行，密钥只留在当前进程环境里：

```bash
DEEPSEEK_API_KEY='你的 DeepSeek Key' uv run evalplat eval suites/deepseek-codex-claude-parallel.yaml --output-dir reports/deepseek-codex-claude
```

## 命令

安装：

```bash
uv python install 3.12
uv sync --python 3.12
uv sync --project harbor --python 3.12 --no-dev
```

配置预检（不创建容器、不调用模型、不启动 Harbor）：

```bash
uv run evalplat eval suites/terminalbench-two-agent-demo.yaml --print-config
```

预期：

```text
planned trials: 12
agents: dsh-flash, codex-mini
tasks: kv-store-grpc, fix-git
global concurrency: 4
per-agent concurrency: 2
comparison: dsh-flash -> codex-mini
```

正式运行：

```bash
uv run evalplat eval suites/terminalbench-two-agent-demo.yaml \
  --output-dir reports/terminalbench-two-agent-demo
```

矩阵是 `2 Agent × 2 Task × 3 Trial = 12 Trial`。全局并发 4，每个 Agent 最多占 2 个槽。Harbor 只启动一次。

Job 中断后调用 Harbor Resume，次数由 `recovery.max_job_resumes` 限制：

```bash
uv run evalplat resume RUN_ID
```

看报告和单条轨迹：

```bash
uv run evalplat report --experiment EXPERIMENT
uv run evalplat inspect TRAJECTORY_ID
```

## Outcome

| 最终 Outcome | 典型事实 |
| --- | --- |
| `PASS` | 可靠 Verifier 全通过；不调用 Judge |
| `FAIL` | 可靠 Verifier 有失败 |
| `TIMEOUT` | `AgentTimeoutError` |
| `INFRA_ERROR` | `EnvironmentStartTimeoutError`、`VerifierTimeoutError`、构建/网络/健康检查等基础设施异常 |
| `UNKNOWN` | Agent 完成但 reward/check 缺失；缺证据时诊断为 `UNDETERMINED` |
| `INCOMPLETE` | 中断且没有最终 `result.json`；缺证据时诊断为 `UNDETERMINED` |

Agent 非零退出但 Verifier 正常完成时，按 Verifier 得到 `PASS/FAIL`。Harbor `_retries` 只作为 Attempt 事件；最终成功只落一个 `PASS` Outcome。`AgentTimeoutError` 不做基础设施重试。

诊断发生在 Outcome 落库之后、比较之前。`FAIL / TIMEOUT / INFRA_ERROR / UNKNOWN / INCOMPLETE` 每条最终 Trial 都有诊断；`KNOWN_FAILURE` 也会诊断。比较标签是独立字段：

```text
Baseline PASS + Candidate 非 PASS → NEW_REGRESSION
Baseline 非 PASS + Candidate 非 PASS → KNOWN_FAILURE
Baseline 非 PASS + Candidate PASS → IMPROVED
Baseline PASS + Candidate PASS → BOTH_PASS
```

Infra 最终错误可以额外使 Gate 失败，但不能替代这四种比较标签。Gate 使用 `k / max_regressions / pass_at_1_drop / cost_increase`。

## 测试

```bash
uv run python -m unittest discover -s tests -v
```

自动测试覆盖配置预检、单 Job 12 Trial、六种 Outcome、比较标签和 Gate。不修改真实 TerminalBench。
