# EvalPlant

EvalPlant 是面向 Coding Agent 的离线评测诊断后台。Harbor 与 DeepSeek Harness 在隔离容器中执行 Benchmark；EvalPlant 消费运行状态、ATIF 轨迹、Verifier 结果和日志，区分 Harness 故障与 LLM 行为错误，最后输出可追溯诊断和工程统计。

```text
Benchmark → Harbor + DSH → attempt events / ATIF / verifier
                │                         │
                ├─ 并发隔离、心跳、重试   ├─ schema 校验与脱敏
                ↓                         ↓
          evalplant observe        确定性 Harness 规则
                                           │未命中
                                结构化事实 + 受约束 Judge
                                           ↓
                         证据回查 → SQLite → inspect / report
```

项目不再研究 Raw、Graph 或 G-RAV 对比，也不伪装成人工金标已经完成。它的核心是把一次性的“LLM 看日志”变成有责任契约、证据边界、长轨迹策略、版本口径和拒答状态的诊断管线。在线用户反馈暂不在本阶段范围内。

## 诊断机制

Harness 使用 7 个稳定代码：`H-E` 执行环境、`H-T` 工具链路、`H-C` 上下文管理、`H-L` 生命周期、`H-O` 可观测性、`H-V` 验证判分、`H-G` 治理限制。确定性规则只处理工具调用缺少返回、结构化截断标记、生命周期不完整、Verifier 缺失和明确基础设施异常等硬事实。规则未命中不代表 Harness 正常，Judge 可以返回 `HARNESS_SUSPECTED`。

LLM 只分 4 类：`L1` 目标理解与规划、`L2` 推理与决策、`L3` 行动与工具使用、`L4` 反馈验证与结束。责任按接口契约划分：模型选错工具、传错参数或忽略正常反馈归 LLM；合法调用在传输、执行、结果注入、生命周期或验证阶段被破坏归 Harness。主要根因采用 first sufficient cause，并同时保存传播、次要因素、最终失败表面和反事实检查。

短轨迹能放进预算时使用 `FULL` 模式，只调用一次 Judge。长轨迹在本地为全部步骤建立确定性索引，只向 Judge 发送有界分段目录、关键原文和日志首尾；Judge 明确缺证据时可按真实步骤号补取一次，最多调用两次。这里没有额外摘要模型，也不会让索引程序直接决定根因。

Judge 输出上限固定为 4096 token、thinking 关闭、temperature 为 0。每次结果记录 Prompt、规则、模型、schema、输入输出上限和配置哈希；报告发现混合配置会明确警告。证据校验能证明引用存在、步骤关联有效、输出结构合规，但不能单靠字符串证明语义因果，因此语义正确性必须由独立人工验收集评估。

## 项目结构

```text
/Users/shaw/eval-plant
├── harbor/                         # 忽略提交的 Harbor 工作副本
├── integrations/harbor-patches/   # 可在干净 Harbor 上重放的 4 个补丁
├── evalplant/
│   ├── cli.py                      # observe / import / inspect / analyze / report
│   ├── core.py                     # schema、脱敏、标准化、结构索引
│   ├── db.py                       # SQLite 迁移、导入和 attempt 状态
│   ├── judge.py                    # 规则、短长轨迹 Judge、证据校验
│   ├── metrics.py                  # 可比性、成本、耗时和行动建议
│   └── evaluation.py               # 人工金标准确率与重复运行稳定性
├── scripts/prepare_tracebench.py  # 下载公开失败轨迹到本地忽略目录
├── examples/demo-job/              # 无 Key 的确定性演示
├── tests/                           # 不调用付费 API 的自动测试
├── DIAGNOSIS_SPEC.md                # 诊断规约 v3
├── SQLITE_DATA_DICTIONARY.md        # 5 张表逐字段说明
├── DELIVERY.md                      # 交付状态、证据和诚实边界
└── PROJECT_LOG.md                   # 持续维护记录
```

## 完整运行

先安装并跑无 Key 演示：

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

真实 Benchmark 运行期间与结束后的完整链路：

```bash
# Harbor 运行；配置已启用 4 并发和基础设施错误最多 2 次重试
cd /Users/shaw/eval-plant/harbor
export DEEPSEEK_API_KEY='你的有效 Key'
./.venv/bin/harbor run --config examples/configs/agents/dsh-minimal-job.yaml

# 另一个终端可反复查看状态；失去心跳超过 90 秒显示 LOST
cd /Users/shaw/eval-plant
uv run evalplant observe harbor/jobs/JOB_NAME \
  --experiment JOB_NAME --output reports/JOB_NAME-status.json

# 结束后导入最终轨迹；历史失败 attempt 只进 attempts，不当成最终轨迹
uv run evalplant import harbor/jobs/JOB_NAME \
  --experiment JOB_NAME --agent-model deepseek-v4-flash

# 先只诊断一条，确认配置后再诊断全批；--force 才覆盖已有结果
uv run evalplant analyze --experiment JOB_NAME \
  --trajectory TRAJECTORY_ID --model deepseek-v4-pro
uv run evalplant analyze --experiment JOB_NAME --model deepseek-v4-pro

uv run evalplant inspect TRAJECTORY_ID
uv run evalplant report --experiment JOB_NAME \
  --output reports/JOB_NAME.json
```

准备公开真实失败轨迹和人工验收集：

```bash
uv run python scripts/prepare_tracebench.py --limit 20
uv run evalplant import data/public/tracebench/cases \
  --experiment tracebench-review
uv run python -m evalplant.evaluation \
  --gold tests/eval_cases/gold.jsonl \
  --predictions reports/run-a.json reports/run-b.json \
  --reviews tests/eval_cases/evidence-reviews.jsonl
```

Tracebench 原始数据只保存在 Git 忽略的 `data/`，其自带错误阶段仅作人工参考，不能冒充 EvalPlant 金标。

## 验收

```bash
uv run python -m unittest discover -s tests -v
harbor/.venv/bin/python -m pytest \
  harbor/tests/unit/agents/installed/test_dsh_minimal.py \
  harbor/tests/unit/test_job_status.py \
  harbor/tests/unit/test_trial_queue_integration.py -q
```

当前自动化代码验收为 EvalPlant 19 项、Harbor 相关 71 项（DSH 适配器 9 项、状态与队列 62 项）。真正的诊断效果仍需最后输入公开真实失败轨迹，由与 Judge 独立的人审核责任、类别、根因步骤和证据支持性；在这一步完成前，只能宣称系统可运行、可审计，不能宣称归因准确率。
