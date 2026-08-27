# EvalPlant SQLite 字段说明

默认工作库是 `data/evalplant.db`，schema 版本为 6，共 8 张业务表。`experiments` 是一次批次，`tasks` 是逻辑考题，`attempts` 是 Harbor 基础设施尝试，`trajectories` 是 Agent 的一次 Trial，`outcomes/checks` 是真实结果与检查项，`steps` 是轨迹索引，`diagnoses` 是失败诊断。`NULL` 表示上游未提供或该字段不适用。

同目录的 `-wal` 和 `-shm` 是 SQLite WAL 并发模式的辅助文件，不是多余数据库，运行中不能删除。

## experiments：评测批次

| 字段 | 含义 |
|---|---|
| `id` | 实验唯一名称和主键。 |
| `agent_model` | 被评测 Agent 的模型。 |
| `judge_model` | 最近分析该实验时使用的 Judge 模型。 |
| `created_at` | 首次创建的 UTC 时间。 |

## attempts：Harbor 的每次执行尝试

| 字段 | 含义 |
|---|---|
| `id` | 实验 ID 与 Harbor trial ID 组合生成的稳定主键。 |
| `experiment_id` | 所属实验。 |
| `job_id` | Harbor job UUID。 |
| `trial_id` | 本次 attempt 的 Harbor UUID；重试会生成新 UUID。 |
| `trial_name` | 多次 attempt 共享的逻辑 trial 名。 |
| `task_name` | Benchmark 原始任务名。 |
| `attempt_number` | 同一 `trial_name` 的第几次尝试，从 1 开始。 |
| `state` | 最近状态：`RUNNING/SUCCEEDED/FAILED/CANCELLED/TIMEOUT/INFRA_ERROR`；`LOST` 是查询时按心跳动态推导，不写死在库中。 |
| `phase` | 最近生命周期事件，例如 `agent-start`、`verification-start`、`heartbeat` 或 `end`。 |
| `retryable` | 这次异常是否命中 Harbor 的重试白名单，SQLite 中 1 为是、0 为否。 |
| `exception_type` | 异常类名；没有异常时为空。 |
| `exception_message` | 脱敏后的异常说明；当前 Harbor 状态流为避免泄密默认不写正文。 |
| `started_at` | 第一次 START 事件时间。 |
| `updated_at` | 最近事件或心跳时间。 |
| `finished_at` | END/CANCEL 时间，运行中为空。 |
| `event_count` | 已消费的该 attempt 生命周期事件数。 |

## tasks：实验内的逻辑任务

| 字段 | 含义 |
|---|---|
| `experiment_id` | 所属实验，与 `task_key` 组成主键。 |
| `task_key` | 跨 Agent 版本配对使用的稳定任务名，通常为“数据集/实例”。 |
| `source_dataset` | Benchmark 或公开数据集名称。 |
| `source_instance_id` | 数据集内实例 ID。 |
| `success_threshold` | reward/check 分数达到该值视为通过，默认 1.0。 |
| `metadata_json` | 不含原始 Prompt 的最小任务来源元数据。 |

## trajectories：逻辑任务的最终运行轨迹

| 字段 | 含义 |
|---|---|
| `id` | 轨迹唯一 ID，优先使用 Harbor result ID。 |
| `experiment_id` | 所属实验。 |
| `task_id` | 实验内唯一存储名，通常为“任务名::trial 名”。 |
| `verdict` | `PASS/FAIL/TIMEOUT/INFRA_ERROR/UNKNOWN/INCOMPLETE`。 |
| `raw_path` | 原始 ATIF 文件绝对路径；数据库不复制原始敏感内容。 |
| `raw_sha256` | 原始 ATIF 的 SHA-256，用来发现文件变化。 |
| `final_patch_path` | Agent 最终补丁路径。 |
| `final_log_path` | Verifier 或 Agent 最终日志路径。 |
| `cost` | Agent 运行费用（美元），不是 Judge 费用。 |
| `api_calls` | Agent 模型 API 调用数。 |
| `base_task_id` | Benchmark 原始任务名。 |
| `trial_name` | Harbor 逻辑 trial 名。 |
| `health_status` | 执行通道状态 `VALID/INFRA_ERROR/INCOMPLETE`，与任务做对做错分开。 |
| `reward` | Verifier 奖励，多奖励时保存平均值。 |
| `raw_event_path` | DSH 原始 session JSONL 路径。 |
| `raw_event_sha256` | 原始 session 文件指纹。 |
| `agent_version` | Agent/Harness 实际版本。 |
| `agent_name` | 产生轨迹的 Agent 名，与 Benchmark 无关。 |
| `model_name` | 运行任务的实际模型。 |
| `started_at` | Harbor 记录的开始时间。 |
| `finished_at` | Harbor 记录的结束时间。 |
| `input_tokens` | Agent 输入 token。 |
| `cache_tokens` | Agent 命中缓存 token。 |
| `output_tokens` | Agent 输出 token。 |
| `environment_setup_seconds` | 容器环境准备秒数。 |
| `agent_setup_seconds` | Agent 准备秒数。 |
| `agent_execution_seconds` | Agent 执行秒数。 |
| `verifier_seconds` | Verifier 执行秒数。 |
| `source_schema_version` | 原轨迹 ATIF 版本；旧格式记 `legacy`。 |
| `canonical_schema_version` | EvalPlant 内部统一 schema 版本。 |
| `adapter_version` | 本次上游到 canonical 的转换器版本。 |
| `source_dataset` | Benchmark/数据集名，例如 `terminal-bench` 或 `dengdan1999/RootSE`。 |
| `source_instance_id` | 数据集内的任务/实例 ID，不含 Agent 名。 |

## outcomes：每次 Trial 的真实结果

| 字段 | 含义 |
|---|---|
| `trajectory_id` | 对应 Trial，也是主键。 |
| `experiment_id` | 所属实验。 |
| `task_key` | 对应逻辑 Task。 |
| `status` | `PASS/FAIL/TIMEOUT/INFRA_ERROR/UNKNOWN/INCOMPLETE`。 |
| `reward` | Verifier 聚合分数；缺失时为空。 |
| `metadata_json` | 脱敏后的结构化 Outcome 元数据，目前保存各 reward。 |
| `created_at` | 本次 Outcome 导入时间。 |

## checks：Outcome 的原子检查项

| 字段 | 含义 |
|---|---|
| `trajectory_id` | 对应 Trial，与 `name` 组成主键。 |
| `name` | 检查项名称；Harbor reward 自动转成 `reward:<name>`。 |
| `kind` | `CODE/LLM/HUMAN`，表示评分来源。 |
| `status` | `PASS/FAIL/UNKNOWN`。 |
| `score` | 可选数值分数。 |
| `weight` | 汇总加权值，必须大于 0。 |
| `source` | Verifier 字段或人工来源。 |
| `evidence` | 脱敏后的简短结果证据，不保存完整敏感轨迹。 |

## steps：脱敏后的轨迹步骤索引

| 字段 | 含义 |
|---|---|
| `trajectory_id` | 所属轨迹。 |
| `step_index` | 原始真实步骤编号。 |
| `role` | 产生者，如 user、agent、tool。 |
| `action_type` | 推理、读写文件、测试、工具错误等导航类型；不直接决定责任。 |
| `content_preview` | 脱敏后的内容预览，最多 12000 字符；Judge 仍从原轨迹构建脱敏输入。 |
| `command` | 脱敏后的 shell 命令。 |
| `test_status` | `passed/failed/unknown`，非测试步骤为空。 |
| `tool_name` | 工具名。 |
| `tool_arguments` | 脱敏后的工具参数 JSON。 |

## diagnoses：每条轨迹的最新诊断

| 字段 | 含义 |
|---|---|
| `trajectory_id` | 被诊断轨迹，也是主键，所以默认只保留最新诊断。 |
| `status` | `ATTRIBUTED/HARNESS_SUSPECTED/UNDETERMINED/INPUT_TOO_LARGE/FAILED`。 |
| `responsibility` | 已确认的主要责任 `HARNESS/LLM`；拒答或怀疑时为空。 |
| `category_code` | Harness `H-E/H-T/H-C/H-L/H-O/H-V/H-G` 或 LLM `L1/L2/L3/L4`。 |
| `category_name` | 类别中文名。 |
| `root_cause_step` | first sufficient cause 对应的真实步骤编号。 |
| `component` | 出错组件，无法确认时为空。 |
| `summary` | 一句话诊断或拒答原因。 |
| `confidence` | `HIGH/MEDIUM/LOW`。 |
| `decision_source` | `RULE` 或 `LLM`。 |
| `matched_rule` | 确定性规则名；LLM 诊断为空。 |
| `judge_model` | 实际 Judge 模型。 |
| `prompt_version` | 实际诊断 Prompt 版本，当前为 `engineering_diagnosis_v3`。 |
| `judge_input_tokens` | 全部 Judge 调用输入 token 总和。 |
| `judge_output_tokens` | 全部 Judge 调用输出 token 总和。 |
| `judge_latency_seconds` | 全部 Judge 调用耗时总和。 |
| `judge_thinking` | 默认 `disabled`，消融实验可记录 `enabled:<effort>`；未调用为 `not_called`。 |
| `judge_max_input_tokens` | 本次输入预算。 |
| `judge_max_output_tokens` | 本次输出上限。 |
| `diagnosis_error` | 调用、解析或校验失败原因。 |
| `rule_version` | 确定性规则版本。 |
| `diagnosis_config_hash` | 模型、Prompt、规则、schema、thinking、temperature 和 token 上限的组合指纹。 |
| `judge_temperature` | 实际 temperature，当前为 0。 |
| `judge_call_count` | Judge 调用次数：规则为 0，短轨迹通常 1，长轨迹最多 2。 |
| `trajectory_mode` | `RULE/FULL/HIERARCHICAL`。 |
| `evidence_validation_level` | `PROVENANCE_AND_CONTRACT`、`PROVENANCE_AND_REPORT_STRUCTURE` 或 `NOT_APPLICABLE`，不会冒充语义因果已被程序证明。 |
| `report_json` | 完整 JSON，含 primary、secondary、causal chain、failure surface、证据、反事实、版本和运行事实。 |
| `created_at` | 保存诊断的 UTC 时间。 |
