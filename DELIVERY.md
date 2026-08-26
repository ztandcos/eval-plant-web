# EvalPlant 实习项目交付说明

## 一句话定位

EvalPlant 是面向 Coding Agent 的离线评测诊断工具：Harbor 与 DeepSeek Harness 负责在容器里执行 Benchmark，EvalPlant 把执行结果统一为可查询数据，区分 Harness 故障与 LLM 行为错误，并输出带原始证据的诊断和统计报告。

它解决的不是“再造一个 Benchmark Runner”，而是任务跑完以后工程团队最常遇到的问题：失败究竟是模型做错了，还是工具、上下文、容器、调度、日志或 Verifier 出了问题。

## 实习工作形成的两层交付

第一层是执行集成。在 Harbor 基线之上接入官方 DeepSeek Harness SDK，新增 `dsh-minimal` Agent，把 SDK 事件转换为 ATIF，记录模型、版本、token、成本和工具调用，同时避免把 API Key 放入进程参数。由于 Harbor 上游源码较大，交付物采用三个可重放 Git 补丁，而不是复制整个仓库。

第二层是诊断平台。EvalPlant 导入 Harbor job，保存实验、轨迹和步骤，先用确定性事实识别明确 Harness 故障，其余失败再将完整轨迹交给一次 LLM Judge。Judge 输出必须符合固定分类和 JSON 结构，引用证据必须能在原始轨迹或日志中逐字找到。

```text
Benchmark
    ↓
Harbor + dsh-minimal + Docker
    ↓
ATIF + result.json + Verifier log
    ↓
EvalPlant import
    ↓
Harness hard rules ──明确故障──→ Harness diagnosis
    │
    └──其余失败──→ one LLM call ─→ evidence validation
                                      ↓
SQLite ─→ inspect / statistics / JSON report
```

## 已交付能力

| 能力 | 当前状态 | 验收证据 |
|---|---|---|
| Harbor 接入 DeepSeek Harness | 完成 | 三个补丁可在干净 Harbor 基线上重新应用 |
| DeepSeek Harness 轨迹转 ATIF | 完成 | Harbor 适配器单元测试覆盖事件和 token 转换 |
| Harbor job 导入 | 完成 | 支持 ATIF、Verifier、运行元数据和重复实验 |
| Harness / LLM 责任分流 | 完成 MVP | 硬规则只相信结构化事实，歧义交给 Judge |
| 错误分类 | 完成 | Harness 7 层，LLM 4 类，代码稳定、中文说明 |
| 单次 Judge 与成本保护 | 完成 | thinking 关闭、输出上限 4096、保守输入估算、超限不调用 |
| 证据防伪 | 完成 | step ID 和 quote 都由程序回查原始输入 |
| 诊断与统计持久化 | 完成 | SQLite 只有 4 张核心表，每条轨迹保留最新诊断 |
| 人工查看与机器导出 | 完成 | Rich 终端查看；`report --output` 导出 JSON |
| 无 Key 可复现演示 | 完成 | `examples/demo-job` 可稳定命中 Harness H-T |

## 当前数据与真实结果

本机工作库保留两个 Terminal-Bench pilot，共 12 次真实任务运行和 285 个轨迹步骤，其中 8 次 PASS、4 次 FAIL。目前只对一条失败轨迹执行了真实 Judge，结果为 `LLM / L4 / HIGH`，输入 8723 token、输出 294 token、耗时 5.61 秒。

这条结果的证据原文全部通过校验，但人工复核认为第 14 步更像症状而非最早根因。该案例促使 Prompt 从 `engineering_diagnosis_v1` 升级到 `engineering_diagnosis_v2`：最终总结不能仅因与 Verifier 冲突就被当成根因；Agent 最后观察正常而 Verifier 随后异常、且中间没有证据时，应返回 `UNDETERMINED`。

因此当前可以证明的是“完整工程链路可运行、数据可审计、证据不可伪造”，不能宣称“归因准确率已经得到证明”。这是本项目刻意保留的诚实边界。

## 验收方式

### 1. 无外部依赖演示

```bash
uv sync
DEMO_DB=/tmp/evalplant-demo.db
uv run evalplant --db "$DEMO_DB" import examples/demo-job \
  --experiment demo --agent-model demo-model
uv run evalplant --db "$DEMO_DB" analyze \
  --experiment demo --model not-called
uv run evalplant --db "$DEMO_DB" report \
  --experiment demo --output /tmp/evalplant-demo-report.json
```

预期结果是 `HARNESS / H-T / missing_tool_result`，诊断 token 为 0。

### 2. EvalPlant 自动测试

```bash
uv run python -m unittest discover -s tests -v
```

测试覆盖四表数据库、ATIF 导入、重复实验 ID、Harness 硬规则、普通 Agent 超时边界、单次 Judge、Prompt 版本、证据防伪、输入超限、报告导出和随仓库交付的演示数据。

### 3. Harbor 定制恢复

按 `integrations/README.md` 在指定 Harbor 基线应用三个补丁。恢复后的 Git tree 应为 `71fedeeb4086ad858599fd825eba4465a44c8303`，随后执行：

```bash
uv run pytest tests/unit/agents/installed/test_dsh_minimal.py -q
```

## 项目边界

当前交付只做离线诊断和统计，不包括 Web 控制台、在线用户反馈、插件生成、自动修复、自动重跑和归因算法竞赛。这些能力并不是忘记实现，而是为了让实习项目形成一条真正可验收的主线而主动删去。

Who&When 轨迹和人工标注作为早期研究资料保留在本机数据目录，不进入当前运行链路。Raw、Graph、G-RAV 和 DeepDebug 的比较代码已删除；早期实验说明单次 Judge 结果存在波动，也推动项目从“发明定位算法”转向“建立可审计诊断机制”。

## 已知限制与下一阶段

最重要的限制是样本复核不足。下一阶段不应先做前端，而应选取 10～20 条真实失败轨迹，由人工复核责任域、主要类别、根因步骤和证据充分性，形成一份小而可信的诊断验收集。只有边界稳定后，才值得批量运行或建设 Web 控制台。

第二个限制是 Judge 费用只能统计 token 和耗时，当前模型接口没有返回本次 Judge 的美元费用，因此系统不会根据未知价格表伪造成本。第三个限制是本机历史数据库中的轨迹路径指向原 Harbor job 目录；交付仓库通过独立 demo 和 Harbor 补丁保证可复现，但历史原始任务数据不随 Git 发布。

## 面试或答辩时的核心表达

这个项目最有价值的部分不是“调用 LLM 看日志”，而是把执行、数据和诊断边界做清楚：执行结果与诊断状态分开，Harness 与 LLM 责任分开，硬事实与模型判断分开，原始证据与摘要分开，Prompt 和 token 配置可审计，证据不足允许不下结论。它展示的是 Agent 评测工程中的可复现性、可观测性、成本控制和诚实评估。
