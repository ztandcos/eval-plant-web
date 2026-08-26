# EvalPlant 实习项目交付说明

## 一句话定位

EvalPlant 是 Coding Agent 的离线评测与故障诊断后台：Harbor 和 DeepSeek Harness 负责可靠执行，EvalPlant 负责把运行状态与失败轨迹转换成可查询、可审计、可比较的诊断结果。它不只是“把日志发给 LLM”，而是把执行隔离、责任契约、结构事实、证据校验、版本口径和人工验收接口连成一条管线。

## 最终链路

```text
Benchmark
  ↓
Harbor + dsh-minimal + container
  ├─ 有界并发、每任务隔离
  ├─ heartbeat / lifecycle JSONL
  └─ 仅基础设施异常重试，旧 attempt 保留
  ↓
ATIF + result.json + verifier log
  ↓
schema fail-closed → canonical adapter → secret redaction
  ↓
Harness precision-first rules
  ├─ 硬契约已被违反 → 规则诊断，Judge 调用 0 次
  └─ 未命中或存在歧义 → 结构化事实 + constrained Judge
       ├─ 短轨迹 FULL，1 次调用
       └─ 长轨迹本地全量索引，最多 1 次按步骤补证据，共最多 2 次
  ↓
证据来源与输出结构校验 → SQLite → inspect / report / evaluation
```

## 已实现与验收证据

| 能力 | 实现状态 | 可核验证据 |
|---|---|---|
| DeepSeek Harness 接入 Harbor | 已实现 | 4 个可重放补丁；适配器保留模型、版本、token、成本和 ATIF 事件 |
| 任务隔离与自动重试 | 已实现 | Harbor 原生 semaphore + retry；重试只作用于失败 trial，旧 attempt 移入 `_retries` |
| 实时运行观测 | 已实现 | START/阶段/HEARTBEAT/END/CANCEL 事件写入 `execution-events.jsonl`；`evalplant observe` 聚合 RUNNING、LOST、TIMEOUT、INFRA_ERROR 等状态 |
| 上游 schema 演进 | 已实现 | 已知 ATIF v1.0–v1.7 才能导入；未知版本拒绝；canonical、adapter 和 SQLite `user_version` 均持久化 |
| 敏感数据保护 | 已实现本地边界 | API Key 不进入进程参数；数据库预览、工具参数和 Judge payload 脱敏；状态流不保存异常正文；原始轨迹不复制进数据库 |
| Harness / LLM 责任边界 | 已实现 | contractual responsibility 写入规约和 Prompt；硬规则只依赖结构化事实 |
| 多因素因果表达 | 已实现 | 一个 primary 用于统计，最多 3 个 secondary，加 causal chain、failure surface 和 counterfactual |
| 长轨迹诊断 | 已实现 | 本地索引所有步骤，发送有界分段目录和关键原文；最多二次 Judge，不调用摘要模型 |
| Judge 成本与漂移控制 | 已实现 | thinking 关闭、temperature 0、输出 4096、输入预算；保存模型/Prompt/规则/schema/配置哈希；混合配置报告告警 |
| 证据校验 | 已实现明确边界 | 程序验证 step、quote、来源、关系字段和因果结构；不把字符串存在冒充语义蕴含 |
| 诊断可信度评测设施 | 已实现，真实标签待输入 | `evaluation.py` 计算 coverage、总体/选择性责任与类别准确率、根因 exact/near、证据支持率和重复运行一致率 |
| 工程决策统计 | 已实现 | 按模型、Agent 版本、责任、类别、组件、token、成本、耗时统计，并把 H/L 分类映射到整改方向 |

当前自动验证结果是 EvalPlant 19 项通过，Harbor 相关 71 项通过，其中 DSH 适配器 9 项、任务状态与队列 62 项。Harbor 四个补丁已在干净基线 `b37833221e27435a18d7acdd41d875cdc2831893` 上重新应用，恢复 tree 指纹为 `bfea9c800c913be0d23225b7f8472a3ac5f06f9e`。

## 当前仍未宣称完成的唯一效果验收

代码设施已经就绪，但“诊断准确”不能由开发者或被测 Judge 自证。最后需要下载公开真实失败轨迹，找独立人工审核责任、类别、first sufficient cause 步骤和证据是否真的支持结论，再固定同一配置重复诊断计算稳定性。Tracebench 下载与转换脚本、金标格式、证据 review 格式和指标程序均已交付；真实人工标签不会由模型伪造。

这意味着面试时可以确定地说“系统已完成并通过工程测试”，也必须诚实地说“目前还没有足够人工样本宣称某个准确率”。早期 12 条本地 Terminal-Bench 运行和一次旧 Prompt Judge 只能证明链路跑通过，不能作为 v3 效果数字。

## 设计边界

SQLite 是刻意选择的单机工作库，WAL 支持当前并发读取和单写入场景。它不是多租户、千万级服务的最终存储；到多进程高频写入时，应把 attempts/metadata 切到 PostgreSQL，把原始轨迹放对象存储。当前没有为尚不存在的规模提前增加 repository 抽象。

系统只做诊断和统计，不让 EvalPlant 控制 Harbor 重试，也不自动修改 Agent、生成插件或上线修复。在线用户反馈按用户要求暂不实现。原始轨迹仍受本机文件权限和 Harbor 生命周期管理；多用户 Web 部署前还需要对象级权限、加密存储和 retention job。

## 面试表达

如果只写脚本，确实可以解析一次 ATIF 并调用 DeepSeek。EvalPlant 的工程价值是：一千次运行之后，仍能区分最终任务和历史失败 attempt，知道结果由哪套 Prompt/规则/schema 产生，找到原始证据，拒绝证据不足的判断，发现不同配置不可比较，并把 H-T、H-C、L3、L4 等统计直接映射到工具链、上下文、模型 tool-use 和完成前验证的整改优先级。这是“可验证边界的诊断系统”，不是“LLM 日志分类器”。
