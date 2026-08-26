# EvalPlant 项目说明与工作记录

更新日期：2026-08-26

项目目录：`/Users/shaw/eval-plant`

当前分支：`codex/harbor-atif-eval`

## 当前目标

EvalPlant 是 Harbor 运行结果之后的一层离线诊断后台：导入 ATIF 轨迹，先用确定性事实判断 Harness 是否故障，其余失败最多调用一次 LLM Judge，最后保存诊断并做统计。系统只做诊断和统计，不做在线反馈、修复建议执行、插件生成、自动重跑或科研式算法对比。

当前唯一完整链路是：

```text
Harbor / DeepSeek Harness → ATIF → import → analyze → inspect / report
```

## 当前实现

- CLI 只保留 `import`、`inspect`、`analyze`、`report`。
- Harness 固定为 ETCLOVG 七层；LLM 固定为四类。
- 硬规则只读取明确结构化事实，不从模型自然语言猜测 Harness 故障。
- 歧义失败使用完整轨迹进行一次 Judge 调用；超过输入上限不截断、不调用。
- 一份诊断只有一个主要责任、一个主要类别和一个根因，最多一个次要因素。
- LLM 证据必须在真实步骤或日志中逐字存在；虚假证据使诊断失败。
- 每条轨迹只保留最新诊断，默认不重复调用，`--force` 才覆盖。
- Judge 故障记录为诊断 `FAILED`，不改写原任务结果。

## 当前数据

唯一工作库为 `data/evalplant.db`，只有 `experiments`、`trajectories`、`steps`、`diagnoses` 四张表。迁移后保留 2 个 Terminal-Bench 实验、12 条真实轨迹、285 个步骤，旧归因结果已清空。每个字段见 `SQLITE_DATA_DICTIONARY.md`。

Who&When 的 184 条公开轨迹、184 份人工标注和 manifest 继续保留，但不参与当前链路。

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

## 早期方向变更记录

项目早期曾比较 Raw、Graph、G-RAV 和 DeepDebug，并用 Who&When 金标计算定位准确率。少量样本中算法结果波动大、成本不可控，而且偏离工程平台目标，因此停止这条路线。保留数据和标注只是为了以后确有需要时可以复查，不再维护对应实验代码。

后续每完成一个实际变更，都继续追加到本文件；桌面旧文档不再维护。
