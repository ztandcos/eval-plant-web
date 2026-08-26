# EvalPlant

EvalPlant 是一个只做离线诊断和统计的 Agent 评测后台。Harbor 与 DeepSeek Harness 负责在容器中运行任务并产出 ATIF 轨迹，EvalPlant 负责导入这些结果、判断失败属于 Harness 还是 LLM、保存证据并汇总统计。

当前不做归因算法对比、人工金标打分、在线反馈、自动修复、插件生成或自动重跑。

```text
Harbor + DeepSeek Harness 运行任务
                ↓
ATIF 轨迹 + result.json + Verifier 日志
                ↓
确定性健康规则（只相信结构化事实）
       ├── 明确 Harness 故障 → 直接生成诊断
       └── 其余失败或歧义       → 完整轨迹只调用一次 Judge
                ↓
证据校验 → SQLite 保存 → 统计报告
```

## 项目结构

```text
/Users/shaw/eval-plant
├── harbor/                    # Harbor 执行引擎和 DeepSeek Harness 适配
├── evalplant/
│   ├── cli.py                 # import / inspect / analyze / report
│   ├── db.py                  # 4 张表及 ATIF 导入
│   ├── core.py                # 轨迹标准化和固定分类
│   ├── judge.py               # Harness 规则、单次 LLM 诊断、证据校验
│   └── metrics.py             # 结果、责任、类别、成本和耗时统计
├── evalplant/diagnosis_prompt.txt # 随 Python 包发布的工程诊断 Prompt
├── examples/demo-job/         # 不需要 API Key 的可复现演示
├── integrations/              # 可移植的 Harbor + DSH 补丁
├── data/evalplant.db          # 唯一工作数据库
├── tests/                     # 不调用付费 API 的自动测试
├── SQLITE_DATA_DICTIONARY.md  # SQLite 每个字段的大白话说明
├── DELIVERY.md                # 实习项目交付说明与验收边界
└── PROJECT_LOG.md             # 持续维护的项目说明和工作记录
```

## 诊断规则

Harness 使用 7 个稳定代码：`H-E` 执行环境、`H-T` 工具链路、`H-C` 上下文管理、`H-L` 运行生命周期、`H-O` 可观测性、`H-V` 验证与判分、`H-G` 治理与限制。规则只处理“工具调用没有返回”“结构化字段明确标记截断”“运行不完整”“缺少 Verifier”“基础设施异常”等硬事实。模型在文字里提到 timeout 或 truncated，不会被规则当成 Harness 故障。

其余失败把完整轨迹交给 Judge 一次。LLM 只分为 4 类：`L1` 目标理解与规划、`L2` 推理与决策、`L3` 行动与工具使用、`L4` 反馈、验证与结束。报告只能有一个主要责任、一个主要类别、一个根因，最多附带一个次要因素。LLM 引用的证据必须能在对应轨迹步骤或日志中逐字找到，否则整次诊断记为 `FAILED`；证据不足则记为 `UNDETERMINED`。

系统不会悄悄截断长轨迹。完整输入超过 `--max-input-tokens` 时记为 `INPUT_TOO_LARGE`，不会调用 Judge。Judge 本身失败也只影响诊断状态，不会篡改原任务的结果或健康状态。

## 使用

先运行不需要 Docker 和 API Key 的完整演示：

```bash
cd /Users/shaw/eval-plant
uv sync

DEMO_DB=/tmp/evalplant-demo.db
uv run evalplant --db "$DEMO_DB" import examples/demo-job \
  --experiment demo --agent-model demo-model
uv run evalplant --db "$DEMO_DB" analyze \
  --experiment demo --model not-called
uv run evalplant --db "$DEMO_DB" report \
  --experiment demo --output /tmp/evalplant-demo-report.json
```

演示会稳定得到 `HARNESS / H-T / missing_tool_result`，Judge token 为 0。真实使用时先让 Harbor 运行任务，再导入它的 job：

```bash
# 1. 导入一个 Harbor job
uv run evalplant --db data/evalplant.db import /path/to/harbor/job \
  --experiment my-run \
  --agent-model deepseek-v4-flash

# 2. 只查看轨迹，不调用模型
uv run evalplant --db data/evalplant.db inspect TRAJECTORY_ID

# 3. 诊断一个实验中的全部失败任务
export DEEPSEEK_API_KEY='你的 Key'
uv run evalplant --db data/evalplant.db analyze \
  --experiment my-run \
  --model deepseek-v4-pro

# 也可以先只诊断一条；已有诊断默认跳过，--force 才覆盖
uv run evalplant --db data/evalplant.db analyze \
  --experiment my-run \
  --trajectory TRAJECTORY_ID \
  --model deepseek-v4-pro

# 4. 查看统计
uv run evalplant --db data/evalplant.db report \
  --experiment my-run \
  --output reports/my-run.json
```

自动测试不会调用真实 Judge：

```bash
uv run python -m unittest discover -s tests -v
```

Harbor 中的 `dsh-minimal` 定制没有把 1.7GB 上游源码复制进主仓库，而是以三个可重放补丁交付，恢复方式见 [integrations/README.md](integrations/README.md)。完整验收结论、真实冒烟结果和已知限制见 [DELIVERY.md](DELIVERY.md)。
