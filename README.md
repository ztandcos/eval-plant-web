# EvalPlant

项目的完整背景、当前状态、实验结果和逐步工作记录统一维护在 [`PROJECT_LOG.md`](PROJECT_LOG.md)。开始新工作前先阅读该文件，完成实际步骤后同步更新正文与文末日志。

EvalPlant 是一套面向 Coding Agent 的评测基础设施。Harbor 负责把任务放进隔离容器、运行 DeepSeek Harness、执行 Verifier；EvalPlant 负责保存和索引轨迹、区分运行故障与模型失败、提取客观证据、做失败归因和统计。

```text
Harbor + DeepSeek Harness Minimal（rc6 / rc7）
                     ↓
      原始 Harness JSONL + ATIF-v1.7 + Verifier
                     ↓
        运行健康度 ─ 任务结果 ─ 确定性证据
                                  ↓
                         单次 DeepSeek Judge
                                  ↓
                    人工校准、对比统计、报告
```

项目现在使用一个顶层工作区：`harbor/` 是任务执行引擎，负责容器、Agent 和 Verifier；`evalplant/` 是分析引擎，负责导入轨迹、失败归因和统计。两者都位于 `/Users/shaw/eval-plant`，不再依赖外部 workspace 路径。

```text
/Users/shaw/eval-plant
├── harbor/       # 运行 Terminal-Bench 和 DeepSeek Harness
├── evalplant/    # 轨迹分析与归因代码
├── data/         # Who&When 与 Terminal-Bench 结果
├── prompts/      # Judge 提示词
└── tests/        # EvalPlant 测试
```

这个仓库不做插件生成、自动改 Prompt、强化学习或失败后的自我进化。它只把“跑得是否正常、任务是否成功、失败在哪里、不同版本差多少”做扎实。

## 关键边界

DeepSeek Harness 使用官方极简模式：只有持久 Bash 和字符串编辑器，没有视觉、Skills 或 MCP。主版本固定为 `deepseek-harness-sdk==0.1.0rc7`，A/B 时显式切换到 rc6。

原始 Harness JSONL 永不改写，ATIF-v1.7 是统一分析格式。Verifier 决定 PASS/FAIL；没有 Verifier、用户反馈或人工结果的在线轨迹记为 UNKNOWN。Judge 只解释已知失败，不负责判定成功。

失败归因不是把同一条长轨迹反复交给 AI 猜。现有 Harbor 单条分析入口会先确定性提取工具错误、重复调用、文件编辑、测试和超时信号，再调用一次 Judge；新的公开数据归因实验则由程序先建 Attribution Graph，随后让 Raw 和 Graph 各用完全相同的两次 Judge 调用做公平对比。

归因标签采用《Why LLM Agents Fail》代码本：3 个阶段、9 个主类别、25 个叶子类别。基础设施、Harness 和环境故障在归因前剔除。

## 安装

本机需要 Python 3.9+、uv 和 Docker：

```bash
uv sync
uv run evalplant --help
```

密钥只放在当前终端环境，不要写进配置或 Git：

```bash
export DEEPSEEK_API_KEY="你的 Key"
```

## 一次真实离线运行

Harbor 适配器位于项目内的 `/Users/shaw/eval-plant/harbor`。下面的 smoke job 会运行一个真实容器，安装 rc7，调用 `deepseek-v4-flash` 创建文件，再由 Verifier 判分：

```bash
cd /Users/shaw/eval-plant/harbor
uv run harbor run \
  -c examples/configs/agents/dsh-minimal-job.yaml \
  --job-name evalplant-dsh-rc7-smoke \
  -y
```

把完整 Harbor job 导入 EvalPlant：

```bash
cd /Users/shaw/eval-plant
uv run evalplant --db data/evalplant-harbor.db import \
  /Users/shaw/eval-plant/harbor/jobs/evalplant-dsh-rc7-smoke \
  --experiment rc7-smoke \
  --agent-model deepseek-v4-flash

uv run evalplant --db data/evalplant-harbor.db report \
  --experiment rc7-smoke
```

EvalPlant 会一起索引 Harness JSONL 的路径和 SHA-256、ATIF 路径和 SHA-256、Verifier 日志、SDK 版本、模型名、开始结束时间、reward、健康状态与标准化步骤。

## 失败归因

只分析“运行健康且结果为 FAIL/TIMEOUT”的轨迹：

```bash
uv run evalplant --db data/evalplant-harbor.db analyze \
  --experiment rc7-failures \
  --model deepseek-v4-pro
```

人工标注同时记录关键步骤、阶段、主类别、叶子类别和证据步骤：

```bash
uv run evalplant --db data/evalplant-harbor.db annotate TRAJECTORY_ID \
  --split test \
  --step 12 \
  --phase repair \
  --category implementation_detail_defects \
  --subcategory control_flow \
  --evidence 10,12,14 \
  --evidence-pass yes
```

建议建立 60 条人工金标失败轨迹：20 条 dev 用于校准 Prompt 和标签理解，40 条 test 保持封存。报告分别给归因准确率和归因覆盖率，ABSTAIN 不伪装成正确归因。

### 公开失败轨迹上的两阶段归因实验

Who&When 的 184 条公开失败轨迹可转换为统一 ATIF 形式，作为当前有金标的步骤归因代理开发集。项目只研究单 Agent，因此转换器把不同专家统一折叠成 `agent`，只把 `Computer_terminal` 保留为 tool，不评估原数据里的多 Agent 责任人。Judge 能读取的轨迹放在 `cases`，正确错误步骤、错误原因和标准答案放在独立的 `labels`；Judge 运行过程不会读取 `labels`。固定哈希切分得到 36 条 dev 和 148 条 test，避免一边调 Prompt 一边偷看测试集。

```bash
git clone --depth 1 https://github.com/ag2ai/Agents_Failure_Attribution.git \
  data/external/Agents_Failure_Attribution

uv run evalplant attribution-prepare \
  'data/external/Agents_Failure_Attribution/Who&When' \
  --output data/who-when
```

两种方法使用同一个 Judge、同一份宽泛失败类型、同一个输入上限，而且每条都只调用两次。`two_pass_v2` 的候选阶段关闭 thinking 并限制为 1024 token，验证阶段开启 high thinking 并限制为 16384 token；实际 Prompt 文件、哈希和调用参数都会写进每条结果。Raw 第一轮看按时间排列的原始步骤；Graph 第一轮看程序生成的短节点、责任 Agent、文件关系和数据复用关系。两者第二轮都回到原始轨迹逐个审核候选，反证可直接否决候选，并明确区分非因果瑕疵、失败症状、根因和事后修复。每一轮 Judge 成功后都会写入输出目录下的 `.checkpoints`；后一步临时失败时，原命令重跑会接着未完成的一轮继续，不会重复支付已经完成的调用。

```bash
uv run evalplant attribution-run data/who-when/cases \
  --output data/who-when/two-pass-v2 --method raw --split dev --limit 1

uv run evalplant attribution-run data/who-when/cases \
  --output data/who-when/two-pass-v2 --method graph --split dev --limit 1

uv run evalplant attribution-compare \
  --raw-results data/who-when/two-pass-v2/raw \
  --graph-results data/who-when/two-pass-v2/graph \
  --labels data/who-when/labels --split dev \
  --output data/who-when/dev-report.json
```

报告同时比较候选步骤 Top-3 召回率、责任 Agent 准确率、精确/相邻步骤准确率、归因覆盖率、证据合法率、token 和耗时，并直接给出 Graph 相对 Raw 的精确步骤准确率变化、相邻步骤准确率变化和输入 token 减少比例。项目的核心结论以“归因是否更准、送给 LLM 的内容是否更少”为准；逐字引用是否合法只作辅助诊断，不阻断结果。比较前会检查 Raw 和 Graph 的题目完全配对。Graph 是普通 Python 数据结构，不需要图数据库，也没有让另一个 LLM 先总结轨迹；每个底层节点仍保留原始步骤引用，可以回查证据。

当前一条 dev 样本的真实调试结果中，Raw 预测第 2 步而金标为第 4 步；Graph 预测第 4 步。Graph 输入从 14,274 降到 9,571 token，减少 32.9%，耗时从 151.6 秒降到 100.0 秒。这只能证明单样本链路和优化方向已经跑通，不能代替多条 dev 的稳定性实验。

## 在线影子评测

在线模式接收“任务结束后的整条轨迹”，不做逐步拦截。服务默认只监听本机：

```bash
uv run evalplant --db data/evalplant-online.db serve \
  --store data/online \
  --port 8787
```

向 `POST /ingest` 发送 JSON：

```json
{
  "experiment": "online-shadow",
  "trajectory": {"schema_version": "ATIF-v1.7", "steps": []},
  "result": {"task_name": "task-1", "verifier_result": {"rewards": {"reward": 0}}},
  "raw_events": "{\"type\":\"request/header\"}\n",
  "verifier_log": "failed"
}
```

已知失败会自动进入 SQLite 队列。一个单独进程处理归因：

```bash
uv run evalplant --db data/evalplant-online.db worker \
  --model deepseek-v4-pro
```

队列只有四个状态：PENDING、RUNNING、DONE、FAILED。当前单机 SQLite + WAL 足够支撑作品集演示；出现多机 worker 或明显锁竞争时再换 PostgreSQL/Redis，不提前引入 Kafka、Celery 或 ClickHouse。

## 正式 rc6 / rc7 对比

第一轮正式实验使用 Terminal-Bench 2.1 的 30 个任务，按公开难度分成 easy/medium/hard 各 10 个。相同模型、相同任务和参数下，rc6 与 rc7 各重复 3 次，共 180 条轨迹，并交错运行以减小滚动模型别名和时间窗口带来的污染。

主指标是官方平均 reward 和“同一任务三次全部成功”的 pass-all-repeats；同时报告“至少一次成功”的 pass@3，不能把它当稳定性。延迟、token、工具错误和重复调用只用于解释差异，不混成一个无法说明含义的综合分。

## 测试

```bash
uv run python -m unittest discover -s tests -v
```

测试覆盖旧轨迹兼容、Harbor ATIF 导入、Verifier/健康状态分离、原始 JSONL 哈希、确定性步骤解析、SQLite 队列和在线已知失败入队。
