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

`suites/terminalbench-two-agent-demo.yaml` 是正式双 Agent 配置：`dsh` 使用 `DEEPSEEK_API_KEY`，`codex` 使用 `OPENAI_API_KEY`。密钥只来自环境变量，不写入 YAML，不提交 Git。

```bash
export DEEPSEEK_API_KEY='你的 DeepSeek Key'
export OPENAI_API_KEY='你的 OpenAI Key'
```

YAML 只支持 `agents`（至少一个）。每个 Agent 可写 `name / agent / model / n_concurrent / agent_kwargs`。可选嵌套 `diagnosis` 和 `recovery`。可选 `comparison`；没有它时就是绝对评测，不生成回归标签。

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
