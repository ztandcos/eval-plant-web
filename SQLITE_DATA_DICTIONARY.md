# EvalPlant SQLite 字段说明

工作库是 `data/evalplant.db`。它只有四张表：一次评测批次放在 `experiments`，每个任务运行放在 `trajectories`，轨迹拆出的步骤放在 `steps`，失败诊断放在 `diagnoses`。空值 `NULL` 表示 Harbor 没提供该数据或该字段不适用于当前记录。

SQLite 同目录偶尔出现的 `evalplant.db-wal` 和 `evalplant.db-shm` 是 SQLite 为安全并发读写自动创建的辅助文件，不是多余数据库；程序关闭后可能消失，不应在运行中手动删除。

## experiments：一次评测批次

| 字段 | 大白话含义 |
|---|---|
| `id` | 实验唯一名字，也是其他表关联这次批次的主键。 |
| `agent_model` | 被评测 Agent 使用的模型；导入时可填写。 |
| `judge_model` | 最近对该实验执行诊断时使用的 Judge 模型。 |
| `created_at` | 这条实验记录首次创建的 UTC 时间。 |

## trajectories：每个任务的一次真实运行

| 字段 | 大白话含义 |
|---|---|
| `id` | 这次轨迹的唯一 ID，优先使用 Harbor `result.json` 中的 ID。 |
| `experiment_id` | 这条轨迹属于哪个实验，对应 `experiments.id`。 |
| `task_id` | 数据库内用于区分重复运行的任务名，通常是“原任务名::trial 名”。 |
| `verdict` | 最终结果：`PASS`、`FAIL`、`TIMEOUT`、`INFRA_ERROR`、`UNKNOWN` 或 `INCOMPLETE`。 |
| `raw_path` | 标准 ATIF `trajectory.json` 的绝对路径。 |
| `raw_sha256` | ATIF 文件的 SHA-256 指纹，用来发现原文件是否被改过。 |
| `final_patch_path` | Agent 最终补丁文件的绝对路径；没有补丁时为空。 |
| `final_log_path` | Verifier 测试日志或 Agent 最终日志的绝对路径；不存在时为空。 |
| `cost` | Harbor 记录的这次 Agent 运行费用，单位美元；不是诊断费用。 |
| `api_calls` | Agent 在这次任务中调用模型 API 的次数。 |
| `base_task_id` | Benchmark 原始任务名，不包含重复运行的 trial 后缀。 |
| `trial_name` | Harbor 为这一次具体运行分配的 trial 名。 |
| `health_status` | 运行通道健康状态，通常是 `VALID`、`INFRA_ERROR` 或 `INCOMPLETE`；它和任务做对做错是两件事。 |
| `reward` | Verifier 奖励，多项奖励存在时保存其平均值。 |
| `raw_event_path` | DeepSeek Harness 原始 `session.jsonl` 路径；没有时为空。 |
| `raw_event_sha256` | 原始事件文件指纹；没有原始事件时为空。 |
| `agent_version` | 实际运行的 Agent/Harness 版本。 |
| `model_name` | 实际运行任务的模型名称。 |
| `started_at` | Harbor 记录的任务开始时间。 |
| `finished_at` | Harbor 记录的任务结束时间。 |
| `input_tokens` | Agent 运行消耗的输入 token。 |
| `cache_tokens` | Agent 运行命中的缓存 token。 |
| `output_tokens` | Agent 运行产生的输出 token。 |
| `environment_setup_seconds` | Harbor 准备容器环境花费的秒数。 |
| `agent_setup_seconds` | 准备 Agent 花费的秒数。 |
| `agent_execution_seconds` | Agent 真正执行任务花费的秒数。 |
| `verifier_seconds` | Verifier 测试和判分花费的秒数。 |

## steps：ATIF 轨迹中的步骤索引

| 字段 | 大白话含义 |
|---|---|
| `trajectory_id` | 这一步属于哪条轨迹，对应 `trajectories.id`。 |
| `step_index` | ATIF 中的真实步骤编号；诊断引用根因时使用它。 |
| `role` | 这一步是谁产生的，例如 user、agent 或 tool。 |
| `action_type` | 系统整理出的动作类型，例如推理、读文件、改文件、执行测试或工具错误；用于展示和统计，不直接决定责任。 |
| `content_preview` | 这一步的可读内容，数据库最多保留 12000 个字符；Judge 读取的是原始 ATIF，不依赖这个预览。 |
| `command` | 这一步执行的 shell 命令；没有命令时为空。 |
| `test_status` | 若这一步是测试，记录 passed、failed 或 unknown；其他步骤为空。 |
| `tool_name` | ATIF 中调用的工具名；没有工具调用时为空。 |
| `tool_arguments` | 工具参数的 JSON 文本；没有参数时通常是 `null`。 |

## diagnoses：每条失败轨迹的最新诊断

| 字段 | 大白话含义 |
|---|---|
| `trajectory_id` | 被诊断的轨迹 ID，也是本表主键，所以一条轨迹只保留最新诊断。 |
| `status` | `ATTRIBUTED` 已归因、`UNDETERMINED` 证据不足、`INPUT_TOO_LARGE` 完整输入超限、`FAILED` 诊断过程失败。 |
| `responsibility` | 主要责任域，只能是 `HARNESS` 或 `LLM`；没法归因时为空。 |
| `category_code` | 稳定错误代码：Harness 为 `H-E/H-T/H-C/H-L/H-O/H-V/H-G`，LLM 为 `L1/L2/L3/L4`。 |
| `category_name` | 错误代码对应的中文名称，方便人阅读。 |
| `root_cause_step` | LLM 根因所在的真实 ATIF 步骤编号；Harness 组件故障或无法确定时可以为空。 |
| `component` | Harness 出错组件名，或 Judge 能明确指出的相关组件；不明确时为空。 |
| `summary` | 一句话中文根因结论。 |
| `confidence` | `HIGH`、`MEDIUM` 或 `LOW`；诊断没完成时为空。 |
| `decision_source` | 结论来自确定性 `RULE` 还是一次 `LLM` Judge。 |
| `matched_rule` | 命中的确定性规则名；LLM 结论时为空。 |
| `judge_model` | 实际调用的 Judge 模型；纯规则结论和未调用模型时为空。 |
| `prompt_version` | 实际使用的诊断 Prompt 协议版本，用来防止只换目录却误以为升级。 |
| `judge_input_tokens` | 本次 Judge 实际输入 token；没调用时为 0。 |
| `judge_output_tokens` | 本次 Judge 实际输出 token；没调用时为 0。 |
| `judge_latency_seconds` | 本次 Judge 请求耗时秒数；没调用时为 0。 |
| `judge_thinking` | 实际配置的思考模式；当前 LLM 调用固定为 `disabled`，未调用时是 `not_called`。 |
| `judge_max_input_tokens` | 本次诊断允许的完整输入 token 上限。 |
| `judge_max_output_tokens` | 本次 Judge 的输出 token 上限；未调用时为 0。 |
| `diagnosis_error` | Judge 请求、JSON 解析或证据校验失败的具体错误；正常诊断为空。 |
| `report_json` | 完整诊断 JSON，包含证据原文、因果链、最多一个次要因素、运行事实和输入上限等细节。 |
| `created_at` | 这份最新诊断保存到数据库的 UTC 时间。 |
