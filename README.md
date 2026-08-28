# EvalPlant

```text
Agent × Bench × Sandbox
        ↓  evalplant bench
Harbor（项目内部执行引擎，用户不直接操作）
        ↓  ATIF + Outcome
EvalPlant 导入 / 归因 / report
```

EvalPlant 是面向 Coding Agent 的 Outcome-first 离线评测与诊断后台。多 Agent、多 Bench 通过 EvalPlant 自己的参数接入 Harbor；用户不执行 `harbor run`。Harbor 在隔离环境中执行任务后，EvalPlant 统一保存逻辑 Task、多次 Trial、真实 Outcome 和确定性 Check，比较 Agent 版本，并对失败轨迹区分 Harness 故障与 LLM 行为错误。

```text
Benchmark Task → Harbor + DSH → 多次 Trial
                       ├─ Transcript：ATIF 轨迹
                       └─ Outcome：Verifier / 文件 / 测试状态
                                      ↓
                        确定性 Checks → 任务与 Trial 指标
                                      ├─ compare：pass@k / pass^k / 回归门禁
                                      └─ 失败轨迹：规则 + 受约束 Judge
                                                     ↓
                                  证据回查 → SQLite → inspect / report
```

项目不再研究 Raw、Graph 或 G-RAV 对比。它的核心是先用 Outcome 和 Check 判断 Agent 是否把任务做成，再把一次性的“LLM 看日志”变成有责任契约、证据边界、长轨迹策略、版本口径和拒答状态的诊断管线。在线用户反馈暂不在本阶段范围内。

## Outcome-first 评测

`base_task_id` 表示逻辑 Task，一条 `trajectory` 表示一次 Trial。导入 Harbor `result.json` 时，Verifier 的每个 reward 会自动成为 `CODE` Check；也可显式提供 `verifier_result.checks`，字段为 `name/kind/status/score/weight/source/evidence`。系统拒绝非法状态、重复名称、非有限分数和非正权重。

`report` 同时给出逻辑任务数、Trial 数、Trial 通过率、至少一次成功的 Task 比例、全部 Trial 稳定成功的 Task 比例和加权 Check 通过率。`compare` 只比较两个实验共有且双方至少有 k 次 Trial 的 Task，输出经验 pass@k、pass^k、成本与延迟变化、改进/回归任务，并在出现回归、pass@k 下降或平均成本上涨超过 20% 时使 Ship Gate 失败。

## 诊断机制

Harness 使用 7 个稳定代码：`H-E` 执行环境、`H-T` 工具链路、`H-C` 上下文管理、`H-L` 生命周期、`H-O` 可观测性、`H-V` 验证判分、`H-G` 治理限制。确定性规则只处理工具调用缺少返回、结构化截断标记、生命周期不完整、Verifier 缺失和明确基础设施异常等硬事实。规则未命中不代表 Harness 正常，Judge 可以返回 `HARNESS_SUSPECTED`。

LLM 只分 4 类：`L1` 目标理解与规划、`L2` 推理与决策、`L3` 行动与工具使用、`L4` 反馈验证与结束。责任按接口契约划分：模型选错工具、传错参数或忽略正常反馈归 LLM；合法调用在传输、执行、结果注入、生命周期或验证阶段被破坏归 Harness。主要根因采用 first sufficient cause，并同时保存传播、次要因素、最终失败表面和反事实检查。

短轨迹能放进预算时使用 `FULL` 模式，只调用一次 Judge。长轨迹在本地为全部步骤建立确定性索引，只向 Judge 发送有界分段目录、关键原文和日志首尾；Judge 明确缺证据时可按真实步骤号补取一次，最多调用两次。这里没有额外摘要模型，也不会让索引程序直接决定根因。

Judge 默认输出上限为 4096 token、thinking 关闭、temperature 为 0；可通过 `EVALPLANT_JUDGE_THINKING`、`EVALPLANT_JUDGE_REASONING_EFFORT` 和 `EVALPLANT_JUDGE_MAX_OUTPUT_TOKENS` 做受控消融。每次结果记录 Prompt、规则、模型、schema、输入输出上限和配置哈希；报告发现混合配置会明确警告。证据校验能证明引用存在、步骤关联有效、输出结构合规，但不能单靠字符串证明语义因果，因此语义正确性必须由独立人工验收集评估。

## 项目结构

```text
evalplant/
│   ├── cli.py
│   ├── harbor_adapter.py
│   ├── core.py
│   ├── db.py
│   ├── judge.py
│   ├── metrics.py
│   └── evaluation.py
├── harbor/                         # 固定内置 Harbor fork，与项目共用 Git 历史
├── scripts/
├── reports/
├── examples/demo-job/
├── tests/
├── COMMAND_GUIDE.md                # 从安装到所有命令的中文实操手册
└── PROJECT_LOG.md
```

## 完整运行

Harbor 已作为固定 fork 直接纳入当前仓库，不再使用嵌套 Git 或补丁文件。EvalPlant 会自动使用 `harbor/.venv/bin/harbor`，正常用户不需要执行 Harbor 命令。

```bash
cd /Users/shaw/eval-plant
uv python install 3.12
uv sync --python 3.12
uv sync --project harbor --python 3.12 --no-dev
```

完整回归评测优先使用 Suite 名或 YAML：

```bash
uv run evalplant eval smoke
uv run evalplant eval coding-agent-regression
```

Suite 会自动复用已登记的 production baseline，运行候选版本，导入 Outcome、CTRF 测试级 Check、成本、延迟和轨迹，按 Task 配对计算 pass@k，只诊断新回归并生成 JSON/Markdown Ship Gate 报告。中断后运行 `evalplant resume RUN_ID`；候选正式发布后用 `evalplant baseline --suite SUITE --set EXPERIMENT --version VERSION` 提升为新基线。PR、Nightly 和 Release 示例见 `.github/workflows/evalplant.yml`，配置字段与边界见 [PLATFORM_PLAN.md](PLATFORM_PLAN.md)。

主命令只描述评测矩阵：

```bash
uv run evalplant bench \
  --agent dsh \
  --bench terminal-bench@2.0 \
  --task kv-store-grpc \
  --sandbox docker \
  --k 1 \
  --concurrency 1 \
  --experiment tbench-kv-store-grpc-dsh
```

重复 `--agent` / `--bench` 可做矩阵。本地题集传目录，远程题集传数据集名。`--print-config` 只生成 Job JSON；`--list` 查看别名。Harbor 在内部为每个 Trial 运行 Verifier；失败结果一落盘就进入归因，整批结束后同一条命令输出总报告。没有 ATIF 的 Agent 仍保存 Outcome 和 Check，失败时明确标记 `UNDETERMINED / trajectory_unavailable`，不会伪造根因。安装、真实 Terminal-Bench 示例、每个命令与全部参数见 [COMMAND_GUIDE.md](COMMAND_GUIDE.md)。

无 Key 离线演示：

```bash
DEMO_DB=/tmp/evalplant-demo.db
uv run evalplant --db "$DEMO_DB" run examples/demo-job \
  --experiment demo --model not-called
```

已有轨迹仍可用 `run`。live job 带 `execution-events.jsonl` 时会边跑边归因。

```bash
uv run evalplant run data/jobs/JOB_NAME --model deepseek-v4-pro
uv run evalplant run JOB_NAME --once
```
需要拆开步骤时仍可用旧命令：

```bash
uv run evalplant import examples/demo-job --experiment demo
uv run evalplant analyze --experiment demo --model not-called
uv run evalplant inspect TRAJECTORY_ID
uv run evalplant report --experiment demo --output /tmp/evalplant-demo-report.json
uv run evalplant observe data/jobs/JOB_NAME --experiment JOB_NAME

# 相同 Task 在两个 Agent 版本中各跑 k 次后做配对比较
uv run evalplant compare \
  --baseline agent-v1 --candidate agent-v2 --k 3 \
  --output data/agent-v1-vs-v2.json
```

准备公开失败轨迹并用金标评估报告：

```bash
uv run python scripts/prepare_tracebench.py --limit 20
uv run evalplant run data/public/tracebench/cases --experiment tracebench-review
uv run python -m evalplant.evaluation \
  --gold tests/eval_cases/gold.jsonl \
  --predictions reports/run-a.json reports/run-b.json \
  --reviews tests/eval_cases/evidence-reviews.jsonl
```

`run --gold gold.jsonl` 会在报告之后接着算准确率。RootSE 转换结果是公开标注轨迹，不是本机跑出来的 Agent 作业；`source_dataset` / `label_source` 会保留数据来源。Tracebench 原始数据只保存在 Git 忽略的 `data/`，其自带错误阶段仅作人工参考，不能冒充 EvalPlant 金标。

## 验收

```bash
uv run python -m unittest discover -s tests -v
```

EvalPlant 自动测试覆盖 bench 配置生成、实时失败归因、Outcome-only Agent 和最终报告；内置 Harbor 测试验证 DSH 适配、状态事件与隔离重试。公开 RootSE 的 102 条人工标注失败轨迹已完成真实验收：系统链路可运行、可审计，但最早根因步骤精确命中仅 16/102，因此不宣称高准确率。脱敏结果见 `reports/`。
