# EvalPlant 项目说明与工作记录

更新日期：2026-08-27

项目目录：`/Users/shaw/eval-plant`

当前分支：`codex/harbor-atif-eval`

## 当前目标

EvalPlant 是 Harbor 运行结果之后的一层 Outcome-first 离线评测与诊断后台：消费任务生命周期事件、ATIF、Verifier Outcome 和 Checks，统计 Task/Trial 结果并比较 Agent 版本，再对失败轨迹执行受约束诊断。系统不做在线反馈、修复执行、插件生成或科研式算法对比；任务隔离和自动重试由 Harbor 执行层负责。

当前唯一完整链路是：

```text
Harbor / DeepSeek Harness → observe / import → outcome checks / compare → analyze → inspect / report
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

## 2026-08-27：RootSE 真实验收与 thinking 配对实验

接入公开 RootSE 的 102 条人工标注失败轨迹，覆盖 4 类 Agent、7 类模型。5268 个 RootSE 交互转换为 10496 个 ATIF 事件，人工最早决定性错误步骤与 Judge 输入分离。使用 `deepseek-v4-flash`、`engineering_diagnosis_v3`、thinking 关闭完成真实归因：单次失败重试后有 70 条通过证据校验、32 条被拒收；原始交互级精确命中 16/102，前后一步命中 25/102。主要偏差是 Judge 选择后续错误补丁或测试失败点，而不是人工标注的最早错误决策。

随后固定抽取 20 条做配对消融，包括 5 条旧命中、10 条后置归因、5 条旧拒收。非 thinking 为 15 条有效、5 条精确命中；thinking-high（16384 输出上限）只有 6 条有效、2 条精确命中，9 条旧有效结果退化为拒收，5 条旧拒收没有一条被救回。有效结果的选择性精确率同为 33.3%，但 thinking 的覆盖率从 75% 降到 30%，有效输出 token 从 15728 增至 58433，中位有效延迟从 6.1 秒增至 82.7 秒。因此当前单调用 Prompt 保持默认关闭 thinking，配置改为环境变量可控且写入配置哈希。

下一阶段不再把“LLM 找根因”当作整个平台，而是按 `Task → Trial → Transcript + Outcome → Checks → Grader → Diagnosis → Report` 建设 Outcome-first 离线评测。优先补充 Task/Trial/Check/Outcome 数据契约和确定性 outcome grader；LLM Judge 只处理开放性判断并保留不确定出口，RootSE 作为 Judge 校准集。之后再做同任务多 trial、版本配对比较、成本与回退门槛，最后形成 capability、regression、adversarial、multi-turn hard 四类可持续任务集。在线业务指标仍不在当前阶段范围内。

## 2026-08-27：Outcome-first 评测与版本门禁完成

SQLite schema 升至 6，在现有 `trajectories=Trial` 和 `base_task_id=Task` 基础上增加 `tasks/outcomes/checks`，没有重复建设新的执行抽象。导入 Harbor 时，Verifier rewards 自动转换为确定性 CODE Check；也支持显式 `verifier_result.checks`，并对名称、类型、状态、有限分数、正权重做 trust-boundary 校验。旧数据库打开时自动从现有 verdict/reward 回填，原始轨迹无需重跑。

`report` 现在同时统计逻辑 Task、Trial、trial pass rate、Task 至少一次成功率、Task 全部 Trial 稳定成功率和加权 Check 通过率，并在机器报告中逐 Trial 导出 Outcome 与 Checks。新增 `compare` 命令，只比较两个实验共有且双方至少有 k 次 Trial 的 Task，计算经验 pass@k、pass^k、成本和 Agent 耗时变化，列出 improved/regressed/unchanged；存在回归、pass@k 下降或平均成本上涨超过阈值时 Ship Gate 失败。

项目版本升级为 0.5.0。真实 RootSE 与 thinking 配对结果的脱敏摘要进入 `reports/`，仓库外不再是唯一证据。EvalPlant 自动测试增至 24 项并通过；Web UI、在线业务指标、PostgreSQL 和全量 150-task suite 仍按明确边界不实现。

## 2026-08-27：Harbor 内部化，bench 成为主命令

Harbor 作为项目内部执行引擎保留，但不向用户暴露 Harbor CLI。`evalplant bench` 用 `--agent` / `--bench` / `--task` / `--sandbox` / `--k` / `--concurrency` 生成 JobConfig，优先使用项目内已打补丁的 Harbor，密钥只以 `${ENV}` 模板传递。命令持续消费 Trial 生命周期事件；每个 Verifier 失败结果落盘后立即导入和归因，全部 Trial 结束后输出统一报告。对于只产生 `result.json`、没有 ATIF 的 Agent，系统仍保存 Outcome/Check；失败时返回 `UNDETERMINED / trajectory_unavailable`，不把缺失轨迹冒充诊断服务异常。原有 `run` / `import` / `observe` 等命令继续用于已有轨迹和排障。EvalPlant 自动测试增至 34 项。
