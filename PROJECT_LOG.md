# EvalPlant 项目说明与工作记录

更新日期：2026-08-25  
项目目录：`/Users/shaw/eval-plant`  
当前分支：`codex/harbor-atif-eval`
最近功能提交：`6eabb76 Move Harbor workspace under project root`

Harbor 已迁入项目目录：`/Users/shaw/eval-plant/harbor`。接手时只需要打开 `/Users/shaw/eval-plant` 一个工作区。Harbor 内部仓库当前分支为 `codex/evalplant-dsh-minimal`，当前提交为 `106f5d75 fix: keep dsh credentials out of process arguments`。

## 文档定位与维护规则

这是项目唯一持续维护的背景说明、当前状态和工作记录。它最初用于 Agent 迁移，从现在开始同时承担项目日志作用。桌面旧文档已移动到这里，不再维护桌面副本。

后续接手者开始工作前必须先阅读本文。每完成一个实际步骤，都要同时完成两件事：更新正文中受影响的“当前状态”，并在文末追加一条带日期的工作记录。记录至少包含本次目标、实际改动、验证结果、遗留问题和唯一下一步；实验还必须记录样本、模型、Prompt 版本、thinking 配置、token 上限、结果路径和主要指标。

只记录真实完成的内容，讨论中的方案必须明确写成“尚未实现”。不得把输出目录名称当作版本，不得把 API Key、Token 或其他凭据写入本文。发生提交时记录提交哈希；发生删除时记录删除范围以及是否可恢复。

## 1. 一句话介绍

EvalPlant 是一套 Agent 评测与失败归因系统。底层用 Harbor 和 DeepSeek Harness 运行真实任务并保存轨迹；EvalPlant 负责整理轨迹、识别运行故障、分析任务为什么失败，并比较不同归因算法的准确率、成本和稳定性。

当前阶段不是训练模型，也不是做复杂的自动进化系统，而是在验证一个最小问题：**能否从一条失败轨迹中筛出少量可疑步骤，同时不漏掉人工标出的错误步骤，并给出忠于轨迹的归因文字。**

## 2. 长期产品方向

项目最终计划分成前后台两部分。

前台是正常的 Agent Web 产品。用户通过 DeepSeek Harness 完成真实任务，系统保存消息、工具调用和结果轨迹。出现明确失败或负面反馈时，将轨迹送入后台分析。

后台同时处理离线评测和在线反馈。离线部分通过 Bench 适配器接入 Harbor，批量运行固定任务；在线部分分析真实用户的不满意点。更后期才考虑把高频失败整理成 Prompt、规则或插件，经过离线校验后静默发布，并生成更新公告。

当前只做其中最基础的一层：**轨迹标准化、失败归因、基线比较和结果统计。** 插件生成、自动更新和持续学习尚未实现，不应提前扩建。

## 3. 当前归因实验的三种方法

三种方法解决同一个问题，输出相同结构，只改变提供给模型的证据。

### Raw Direct 基线

直接把完整原始轨迹按时间顺序交给 LLM，让它找出所有可能导致失败的步骤并写归因。优点是信息完整；缺点是轨迹长、噪声多、成本高，模型容易把很早的普通瑕疵误判为根因。

### Graph Attribution 基线

程序先把轨迹转换成图。图中包含步骤、工具结果、先后关系、工具返回、数据复用和文件关系。LLM 只看图，不看完整原文。优点是输入少；缺点是确定性图规则不一定准确，而且压缩后可能丢失关键语义。图边只能作为线索，不能当作事实。

### G-RAV（项目方法）

第一阶段使用与 Graph 基线完全相同的图和提示词生成候选步骤；第二阶段再取出候选附近的原始轨迹，让模型根据原始证据保留、否决或暂时保留候选，并生成归因文字。它的核心思想是：**图负责缩小调查范围，原始轨迹负责最终定罪。**

候选数量不固定，不能硬编码为 Top-3 或 3～5 个。确定性程序也不能直接判定谁是根因，只能帮助召回和组织证据。

## 4. 已确定的最小训练任务

现阶段先不做复杂错误分类，也不要求本地模型精确区分首错、决定性失败、传播和症状。只验证算法是否具备候选召回能力。

### 输入

Raw 方法输入完整轨迹；Graph 方法输入图；G-RAV 输入图和候选附近的原始证据。它们共享以下基本信息：

```json
{
  "task": "用户原本要完成什么",
  "outcome": "任务最后如何失败",
  "trajectory_or_graph": "该方法能够看到的证据"
}
```

### 中间过程

模型阅读对应证据，找出所有可能与最终失败有因果关系的步骤。数量不固定，证据不足但可能相关时可以保留，优先保证不漏掉真正错误。程序随后读取独立 labels，检查人工步骤是否出现在候选里。模型运行时绝对不能读取 labels。

### 统一输出

```json
{
  "suspects": [
    {
      "step_id": 4,
      "attribution": "该步骤写入了无法处理空搜索结果的代码，随后在第5步触发异常。",
      "evidence_step_ids": [4, 5]
    },
    {
      "step_id": 6,
      "attribution": "该步骤虽然修复了异常，但继续使用不完整候选名单。",
      "evidence_step_ids": [6, 7]
    }
  ],
  "overall_attribution": "任务失败源于搜索代码不可靠以及候选范围不完整。"
}
```

程序从 `suspects` 提取候选 ID，并计算：

- `candidate_hit`：人工步骤是否包含在候选中。
- `candidate_recall`：一批样本的人工步骤召回率。
- `candidate_ratio`：候选数量除以全部可选步骤数量。

如果只看召回率，模型可以输出全部步骤并得到 100%，因此必须同时报告 `candidate_ratio`。当前阶段不需要把二者强行合成一个复杂指标。

## 5. 三种方法给本地 DeepSeek 的提示词

共同规则保持一致：

```text
请从失败任务中找出所有可能导致最终失败的步骤。

要求：
1. 候选数量不固定。
2. 不要因为步骤看起来不完美就选择它，必须与最终失败有关。
3. 不确定但可能有关的步骤可以保留，优先保证不漏掉真正错误。
4. 对每个候选写明做错了什么、如何影响后续，并给出 evidence_step_ids。
5. 不要编造轨迹中不存在的事实。
6. 只能使用真实存在的 step_id，只输出规定 JSON。
```

Raw 适配部分：

```text
下面是完整原始轨迹，请按照时间顺序阅读。
任务：{task}
失败结果：{outcome}
完整轨迹：{trajectory}
```

Graph 适配部分：

```text
下面是程序构建的轨迹关系图。
任务：{task}
失败结果：{outcome}
图节点：{nodes}
图关系：{edges}
图关系只是线索，不一定完全准确。
```

G-RAV 第一阶段必须与 Graph 使用完全相同的图提示词和候选。第二阶段增加：

```text
下面是第一阶段候选及其附近的原始轨迹。
候选步骤：{candidate_step_ids}
原始证据：{raw_evidence_packets}

请逐个核查：有因果关系就保留；有明确反证就删除；证据不足则保留并说明不确定，避免漏掉真正错误。
```

## 6. 后续正式 DeepSeek Judge

本地模型只负责生成候选和归因文字。以后再用云端 `deepseek-v4-pro` 独立检查这些归因文字是否真实。Judge 不需要重新做整条归因，也不应该替程序计算金标命中。

Judge 对每条归因使用三种结论：

- `SUPPORTED`：原始轨迹直接支持。
- `UNSUPPORTED`：说法可能合理，但证据不足。
- `CONTRADICTED`：与轨迹冲突，属于错误归因或编造。

建议输出：

```json
{
  "claim_reviews": [
    {
      "step_id": 4,
      "verdict": "SUPPORTED",
      "faithfulness_score": 95,
      "reason": "第4步代码确实在第5步触发空值迭代异常。"
    }
  ],
  "overall_faithfulness_score": 95
}
```

以后可以用简单的 100 分制：人工步骤召回 50 分，归因忠实度 30 分，因果说明质量 20 分。`candidate_ratio` 单独报告，不塞进总分。正式 Judge 阶段尚未实现，也不要现在调用 API。

## 7. 数据现状

Who&When 已转换 184 条失败轨迹：

- `data/who-when/cases`：184 条模型可读轨迹。
- `data/who-when/labels`：184 份独立人工标签。
- 固定哈希切分：36 条 dev，148 条 test。
- 轨迹和金标物理分开，Judge 输入不包含人工步骤、错误理由和正确答案。

当前 5 条开发样本是 103、11、110、113、114，人工步骤分别是 4、7、6、8、2。

原始 Who&When 金标只给了一个 `mistake_step`、错误 Agent、简短原因和正确答案，没有说明这个步骤属于首个因果错误还是决定性失败。曾讨论增加错误角色，但用户已明确暂停；目前 5 份 label 文件均未写入角色标注，不要擅自继续。

旧的 `dev5-validation`、`student-run`、`two-pass-v2` 结果目录及对应报告已移入 macOS 废纸篓。184 条 cases、184 条 labels 和外部数据源都保留。

项目已经进一步完成目录清理：旧 BugsInPy Bench、约 4.7GB 的 `.workspaces`、Agent 红队 Bench、陪伴 Bench，以及对应的 raw/oracle/smoke 数据和旧数据库都已移入 macOS 废纸篓。Ruff 和 Python 缓存也已删除。现在 `data` 只保留：

- `data/who-when`：当前归因 Bench、labels 和 5 条预实验结果。
- `data/external/Agents_Failure_Attribution`：Who&When 原始数据源。
- `data/tbench21-pilot.db` 及其 WAL/SHM：Terminal-Bench 2.1 结果。

原 `/Users/shaw/workspace/harbor-dsh-evalplant` 已整体移动到 `/Users/shaw/eval-plant/harbor`，旧路径已不存在。`/Users/shaw/workspace` 下的其他独立个人项目没有被修改。

## 8. 已完成的真实云端预实验

项目曾用 `deepseek-v4-pro`、关闭 thinking、最大输出 4096 token，对上述 5 条分别运行 Raw Direct、Graph Attribution、G-RAV，共得到 15 份有效结果。结果位于：

`data/who-when/universal-dev5-deepseek-v4-pro/`

报告为：

`data/who-when/universal-dev5-deepseek-v4-pro/report.json`

当时使用的是较复杂的 `universal_attribution_v1` 输出，不是现在最终确定的简化候选任务，所以只能作为链路验证，不能当作下一轮正式结论。

已有结果如下：

| 方法 | 当时的精确步骤准确率 | 候选召回 | 总 token | 总耗时 |
|---|---:|---:|---:|---:|
| Raw Direct | 0% | 100% | 34,917 | 65.0 秒 |
| Graph Attribution | 40% | 100% | 21,955 | 66.0 秒 |
| G-RAV | 60% | 100% | 40,419 | 65.8 秒 |

候选召回 100% 的原因是当时直接把全部 Agent 步骤作为候选，属于高召回但没有筛选价值的实现。不能拿它证明候选算法已经有效。G-RAV 当时也比 Raw 多约 15.8% token，后续应通过局部原始证据包解决。

第一次云端 Pro 请求因为没有把 DeepSeek 专用 `thinking: disabled` 真正传入 API，返回空内容；随后修正，15 次有效调用全部完成。API Key 没有写入项目或结果，但曾在对话中明文发送，建议用户轮换。迁移文档不得保存该 Key。

## 9. 本地模型情况

机器是 M1 Pro、32GB 内存，Ollama 已安装。可用模型包括：

- `qwen3.5:9b`
- `deepseek-r1:14b`
- `qwen2.5:3b`
- `gemma3:4b`

`qwen3.5:9b` 的极小 JSON 冒烟测试约 9 秒，但完整复杂归因请求接近 90 秒且输出了未知候选步骤，因此不适合当前复杂 Judge。`deepseek-r1:14b` 尚未真实运行这 5 条，但硬件可以承载。当前共识是让它只做简化候选召回与短归因，配置建议为：

- `think: false`
- `temperature: 0`
- 固定 seed
- 输出上限 512～1024 token

本地 DeepSeek 是否速度和格式都可接受仍需用 1 条样本冒烟验证，不能在文档中宣称已经跑通。

## 10. 当前代码结构

- `harbor/`：内置的 Harbor 执行工作区，负责 Terminal-Bench、DeepSeek Harness、容器和 Verifier。它保留自己的 Git 历史和 `.venv`，主仓库通过 `/harbor/` 忽略规则避免把 1.7GB 依赖重复提交。
- `evalplant/core.py`：轨迹标准化和确定性信号提取。
- `evalplant/attribution_bench.py`：Who&When 转换、确定性建图、旧两阶段归因、临时 universal 三方法预实验和统计。
- `evalplant/cli.py`：命令行入口。目前公开的 `attribution-run/compare` 仍主要对应旧 `raw/graph + two_pass_v2` 流程；新的简化三方法训练任务尚未接入正式 CLI。
- `evalplant/judge.py`：原有 Harbor 失败轨迹 Judge。
- `evalplant/db.py`：SQLite 数据、标签和在线归因队列。
- `evalplant/metrics.py`：实验统计。
- `evalplant/online.py`：在线轨迹接收和排队。
- `evalplant/bugsinpy.py`：BugsInPy/Harbor 任务准备与运行。
- `evalplant/companion.py`：遗留的陪伴评测代码仍在，但对应 Bench 和数据已经删除，不属于当前工作范围。
- `prompts/attribution_candidates_v1.txt`、`v2` 和 verify 文件：历史提示词。
- `prompts/attribution_universal_v1.txt`：云端 5 条预实验使用的复杂提示词，不是刚确定的简化候选提示词。

当前测试共 12 项，全部通过：

```bash
cd /Users/shaw/eval-plant
uv run python -m unittest tests.test_pipeline tests.test_universal_attribution
```

## 11. Git 状态与注意事项

当前工作树干净，所有项目改动已经提交：

```text
099d0ed Add canonical project work log
6eabb76 Move Harbor workspace under project root
1272049 Add attribution benchmark and remove unused benches
```

分支没有配置 upstream，本次提交尚未推送远端。

README 中仍有旧 `two_pass_v2` 和 Top-3 描述，已经落后于最新讨论。不要根据 README 直接继续批量运行，也不要把输出目录名称当成真实 Prompt 版本。每次结果必须记录实际 Prompt 版本、Prompt 哈希、模型、thinking 配置和输出上限。

## 12. 交给下一个 Agent 的唯一下一步

不要继续设计角色体系，不要跑 36 条，不要调用云端 DeepSeek，也不要重构项目。

只完成一个最小闭环：

1. 复用现有轨迹标准化和建图函数。
2. 按本文第 5 节实现三种方法的简化提示词与统一输出。
3. 用本地 `deepseek-r1:14b` 对样本 103 做一次冒烟测试，限制输出 512～1024 token。
4. 验证 JSON、step ID 和证据 ID 都合法。
5. 冒烟通过后再跑固定 5 条，统计每种方法的 `candidate_hit`、`candidate_recall`、`candidate_ratio`、耗时和 token。
6. 展示每条候选及归因文字，让用户先确认是否符合直觉。

这一轮的成功标准只有两个：人工步骤尽量不漏；候选不能退化为全部步骤。归因文字的真假暂时只保存，等用户确认后再用云端 `deepseek-v4-pro` 统一评分。

## 13. 工作记录

### 2026-08-25：完成归因预实验与最小目标收敛

目标：验证 Raw Direct、Graph Attribution 和 G-RAV 三种证据方式能否真实运行，并确定下一轮最小任务。

实际完成：使用 `deepseek-v4-pro`、`thinking: disabled`、4096 输出上限，对固定 5 条 dev 分别运行三种方法，共保存 15 份有效结果；新增 `universal_attribution_v1` Prompt、三方法临时运行逻辑和最小校验测试。结果位于 `data/who-when/universal-dev5-deepseek-v4-pro/`。随后将下一轮目标收敛为“本地模型召回人工错误步骤并输出短归因”，角色标注方案暂停，5 份 label 未被修改。

验证：12 项单元测试通过；仓库未发现明文 API Key。预实验中 G-RAV 精确步骤 60%、Graph 40%、Raw 0%，但三者候选召回 100% 是因为候选覆盖全部 Agent 步骤，不能证明筛选算法有效。

遗留问题：简化候选任务尚未实现；本地 `deepseek-r1:14b` 尚未在这 5 条上真实运行；云端归因真假 Judge 尚未实现。

### 2026-08-25：清理无关 Bench、工作区和缓存

目标：只保留当前 Who&When 归因 Bench 和 Terminal-Bench 2.x，降低目录噪声。

实际完成：将旧 BugsInPy Bench、约 4.7GB 的 `.workspaces`、Agent 红队 Bench、陪伴 Bench、对应 raw/oracle/smoke 数据、旧数据库、Ruff 缓存和 Python 缓存移入 macOS 废纸篓；保留 `data/who-when`、Who&When 外部源和 `data/tbench21-pilot.db`。删除内容可从废纸篓恢复。

验证：清理后仍有 12 项测试通过。提交为 `1272049 Add attribution benchmark and remove unused benches`。

### 2026-08-25：合并 EvalPlant 与 Harbor 顶层工作区

目标：后续 Agent 只打开一个项目目录，不再在 `/Users/shaw/workspace` 和 EvalPlant 之间切换。

实际完成：将原 `/Users/shaw/workspace/harbor-dsh-evalplant` 整体移动到 `/Users/shaw/eval-plant/harbor`；README、忽略规则和运行路径已经更新。Harbor 保留自己的 Git 历史和 `.venv`，主仓库忽略 `/harbor/`，避免重复提交 1.7GB 内容。

验证：移动后 `uv run harbor --help` 成功，启动脚本已使用新路径 `/Users/shaw/eval-plant/harbor/.venv/bin/python3`；EvalPlant 主仓库和 Harbor 内部仓库均为干净状态。提交为 `6eabb76 Move Harbor workspace under project root`。

唯一下一步：按照第 12 节实现简化候选输出，先用本地 `deepseek-r1:14b` 对样本 103 做一次低输出上限冒烟测试，不调用云端模型。

### 2026-08-25：建立项目根目录唯一工作记录

目标：把一次性迁移说明变成后续 Agent 持续维护的项目记录，消除桌面和项目内两份文档可能产生的分叉。

实际完成：桌面 `EvalPlant项目迁移说明.md` 已移动为项目根目录 `PROJECT_LOG.md`；文档增加维护规则、历史步骤和统一日志模板；README 增加入口说明。桌面不再保留或维护副本。

验证：根目录文档存在，桌面旧文件不存在，文档未包含 API Key，主仓库提交后保持干净。初始提交为 `099d0ed Add canonical project work log`。

唯一下一步：后续任何实际改动都先更新正文当前状态，再在本节末尾追加日期记录。

### 后续记录模板

```text
### YYYY-MM-DD：步骤名称

目标：
实际完成：
验证：
结果或提交：
遗留问题：
唯一下一步：
```
