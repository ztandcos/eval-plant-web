# EvalPlant 项目说明与工作记录

更新日期：2026-08-26

项目目录：`/Users/shaw/eval-plant`

当前分支：`codex/harbor-atif-eval`

## 当前目标

EvalPlant 是 Harbor 运行结果之后的一层离线诊断后台：消费任务生命周期事件并导入 ATIF，先用确定性事实判断 Harness 是否有已证实故障，其余失败由受约束 Judge 分析，最后保存诊断并做统计。系统只做诊断和统计，不做在线反馈、修复执行、插件生成或科研式算法对比；任务隔离和自动重试由 Harbor 执行层负责。

当前唯一完整链路是：

```text
Harbor / DeepSeek Harness → observe / ATIF import → analyze → inspect / report
```

## 当前实现

- CLI 为 `observe`、`import`、`inspect`、`analyze`、`report`。
- Harness 固定为 ETCLOVG 七层；LLM 固定为四类。
- 硬规则只读取明确结构化事实，不从模型自然语言猜测 Harness 故障。
- 歧义失败使用“结构化事实 + 原始证据”的 Judge；短轨迹一次调用，长轨迹使用本地全量索引并最多补证据一次。
- 一份诊断只有一个主要责任、类别和 first sufficient cause 用于统计，同时可保留最多三个次要因素和完整因果链。
- LLM 证据必须在真实步骤或日志中逐字存在；虚假证据使诊断失败。
- 每条轨迹只保留最新诊断，默认不重复调用，`--force` 才覆盖。
- Judge 故障记录为诊断 `FAILED`，不改写原任务结果。

## 当前数据

默认工作库为 `data/evalplant.db`，包含 `experiments`、`attempts`、`trajectories`、`steps`、`diagnoses` 五张表。历史本机数据是否存在不作为 Git 交付的一部分；每个字段见 `SQLITE_DATA_DICTIONARY.md`。

## 2026-08-26：工程诊断机制落地

完成新的 Harness-first 诊断链路、一次 Judge 协议、输出上限、证据校验、统计和四命令 CLI。真实轨迹规则体检发现模型文本中的 `truncated` 会造成误判，因此删除了自然语言关键词判断，只保留结构化截断标志或明确基础设施异常。

自动测试共 10 项，覆盖数据库表、Harbor ATIF 导入、Harness 硬规则、普通 Agent 超时边界、单次 Judge 调用、证据防伪、输入过大保护、真实文本误判回归、统计和 CLI 范围。当前环境没有 `DEEPSEEK_API_KEY`，所以没有产生真实 Judge 费用；设置 Key 后应先用 `--trajectory` 只跑一条。

旧的 BugsInPy、陪伴 Agent、在线接收、Raw/Graph/DeepDebug、人工金标评测和旧测试已从当前代码删除。旧数据库与实验产物移动到了 macOS 废纸篓，仍可恢复：

- `/Users/shaw/.Trash/evalplant-db-cleanup-20260826/`
- `/Users/shaw/.Trash/evalplant-cleanup-20260826/`

## 2026-08-26：第一次真实 Judge 冒烟

对 `tbench21-pilot-rc6` 的 `terminal-bench/kv-store-grpc` 运行了一次 `deepseek-v4-pro`。调用成功，结果记录为 `LLM / L4 / HIGH`，根因步骤是 14；实际输入 8723 token、输出 294 token、耗时 5.61 秒，Prompt 版本为 `engineering_diagnosis_v1`，thinking 为 `disabled`，输入/输出上限为 100000/4096。

两条证据都通过了逐字校验，但人工复核发现结论仍有争议：第 13 步刚确认过 `server-alive`，Verifier 随后才发现端口未监听，因此第 14 步的完成声明更像症状，不一定是最早根因。真实问题可能位于 Agent 到 Verifier 之间的进程生命周期，也可能是 Agent 没有采用可持续的服务启动方式；现有证据不足以可靠区分。这次冒烟证明工程链路和证据防伪可以工作，但不证明单次 Judge 的因果判断必然正确。

## 2026-08-26：按实习项目完成交付收口

新增不需要 API Key 的 `examples/demo-job`，已端到端验证 import、Harness H-T 规则、统计和 JSON 报告导出。`report` 新增 `--output`；重复导入同一 Harbor run 到不同实验时会生成稳定的新 ID；token 输入估算从偏乐观的字符数除以 4 改为保守的字符数除以 2。

Prompt 正式升级为 `engineering_diagnosis_v2`。新规约明确：执行型任务的最终完成声明通常是状态描述而非根因；最后一次 Agent 观察与 Verifier 矛盾且缺少中间生命周期证据时应返回 `UNDETERMINED`。历史真实诊断继续保留其实际使用的 v1 版本，不伪装成 v2 结果。

Harbor 仍作为本地独立工作副本，避免把 1.7GB 上游仓库提交进 EvalPlant；三笔 `dsh-minimal` 定制提交已导出到 `integrations/harbor-patches`。在临时干净 Harbor 基线重新应用后，Git tree 与本机一致，适配器 9 项测试全部通过。

新增 `DELIVERY.md` 作为实习答辩与移交说明。EvalPlant 当前自动测试 12 项，覆盖演示样本、数据库、导入、诊断边界、一次 Judge、证据校验、成本保护和报告导出。

最终包构建时发现根目录 Prompt 不会自动进入 wheel，因此将 Prompt 移到 `evalplant/diagnosis_prompt.txt` 并声明为 package data。重新构建并在仓库外的隔离虚拟环境安装后，0.3.0 wheel 可以正确读取 Prompt。早期遗留的 `data/external/Agents_Failure_Attribution`、构建缓存和临时包均已移动到废纸篓 `/Users/shaw/.Trash/evalplant-delivery-cleanup-20260826/`，可恢复。

## 2026-08-26：清理 Who&When 旧资料

删除不再参与当前诊断链路的 `data/who-when`，共 369 个文件、约 7.7 MB，并同步更新 README 和交付说明。原目录已移动到 macOS 废纸篓 `/Users/shaw/.Trash/evalplant-who-when-20260826/`，需要时仍可恢复。

## 早期方向变更记录

项目早期曾比较 Raw、Graph、G-RAV 和 DeepDebug，并用 Who&When 金标计算定位准确率。少量样本中算法结果波动大、成本不可控，而且偏离工程平台目标，因此停止这条路线；对应实验代码、数据和标注均已从项目清除。

后续每完成一个实际变更，都继续追加到本文件；桌面旧文档不再维护。

## 2026-08-26：诊断规约 v3 与执行可观测性完成

诊断从 v2 升级到 `engineering_diagnosis_v3`。新增 contractual responsibility、first sufficient cause、primary/secondary/failure surface/causal chain/counterfactual、`HARNESS_SUSPECTED` 和长轨迹 `NEED_MORE_EVIDENCE` 内部状态。短轨迹仍是一次 Judge；长轨迹不调用摘要模型，而是由程序为全部步骤建立索引，向 Judge 提供有界分段目录和关键原文，按真实步骤号最多补取一次。Judge thinking 固定关闭、temperature 为 0、输出上限 4096，并保存完整配置哈希。

ATIF 导入改为未知版本 fail-closed，保存 source/canonical/adapter 版本；SQLite schema 升至 4。数据库预览、工具参数和 Judge payload 会脱敏。报告记录配置哈希并在混合诊断配置时警告，同时把错误统计映射为工程整改方向。人工评测工具可计算 coverage、总体与选择性准确率、根因 exact/near、证据人工支持率和重复运行一致率；公开 Tracebench 失败轨迹可由脚本下载到本地，但其源注释不冒充 EvalPlant 金标。

Harbor 第四个补丁加入 trial 生命周期 JSONL 和 30 秒心跳，失败 attempt 在重试前归档而不是删除。Harbor 原生有界并发确保单 trial 异常不终止整个 job，重试配置只允许基础设施或瞬态服务异常。EvalPlant 新增 `attempts` 表和 `observe` 命令，同一逻辑任务可查看多次尝试，运行中超过阈值未收到心跳会显示 `LOST`。状态写盘失败只告警，不反向中断任务；重试归档重名时使用 attempt ID 避免阻断。第四补丁在干净 Harbor 基线上重放通过，tree 指纹为 `bfea9c800c913be0d23225b7f8472a3ac5f06f9e`。

代码自动验收为 EvalPlant 19 项、Harbor 相关 71 项（DSH 适配器 9 项、状态与队列 62 项）。版本升级到 0.4.0。剩余工作只是真实效果验收：输入公开失败轨迹，由独立人工形成 review/gold 后运行评测；在此之前不报告虚构准确率。
