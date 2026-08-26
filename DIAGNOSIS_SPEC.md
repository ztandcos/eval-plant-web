# EvalPlant 诊断规约 v3

EvalPlant 的目标不是保证每条失败都能归因，而是在可验证证据范围内区分 Harness 与 LLM 责任。轨迹、日志和模型输出都是不可信输入；证据不足时必须拒绝判断。

## 责任契约

模型选择错误工具、生成不符合工具 Schema 的参数，或忽略正常返回的错误，责任属于 LLM。合法调用在序列化、传输、执行、结果注入、生命周期或 Verifier 阶段被 Harness 破坏，责任属于 Harness。责任由谁违反接口契约决定，不由最后在哪里出现 `error` 决定。

## 根因和因果链

主要根因采用 first sufficient cause：时间上最早、足以导致最终失败、修复后大概率可以阻断失败链的可行动事件。`TRIGGER` 是主要根因，`PROPAGATION` 是错误传播，`SECONDARY` 是促成因素，`FAILURE_SURFACE` 是最终可见失败。两个独立原因都足以导致失败且无法排序时返回 `UNDETERMINED`，不能强行单选。

最终报告保留一个主要责任和类别用于统计，同时在 `causal_chain` 中保存完整过程。最终报错通常是 failure surface，报错后的动作通常是修复尝试，二者都不能仅凭时间靠后被当作根因。

## 诊断状态

- `ATTRIBUTED`：证据足以支持一个主要根因。
- `HARNESS_SUSPECTED`：存在 Harness 候选，但现有结构化事实不足以形成高精度规则。
- `UNDETERMINED`：责任、因果或证据不足。
- `INPUT_TOO_LARGE`：连无损结构索引都无法放入配置的输入上限，未调用 Judge。
- `FAILED`：诊断程序或 Judge 调用失败，不改变原任务结果。

`NEED_MORE_EVIDENCE` 只用于长轨迹第一次 Judge 的内部响应。系统补充指定步骤后最多再调用一次；第二次仍不足则落为 `UNDETERMINED`。

## 证据等级

存在性校验确认 step、quote 和原始来源真实存在；结构校验确认工具调用/返回、Verifier 和明确错误字段满足对应契约；语义和反事实因果不能由字符串匹配证明，必须通过人工验收集校准。模型生成的摘要不能作为最终证据，最终证据必须引用原始步骤、原始日志或结构化运行事实。

## 短轨迹和长轨迹

能在输入预算内包含完整时间线的轨迹使用 `FULL` 模式，只调用一次 Judge。超出预算的轨迹使用 `HIERARCHICAL` 模式：程序在本地为全部步骤建立结构索引，向 Judge 提供有界分段目录以及异常、测试和相关上下文原文；默认调用一次，明确缺证据时按真实步骤号最多补充一次。分段目录由程序统计生成，不增加摘要模型调用。结构索引只负责导航，不限制 Judge 的候选范围，也不直接决定责任。

## 版本可比性

每份诊断必须记录 source schema、canonical schema、adapter、rule、Prompt、Judge 模型、thinking、temperature、输入输出上限和配置哈希。不同配置的结果不能静默混合为同一统计口径。
