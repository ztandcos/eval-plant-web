# EvalPlant

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

这个仓库不做插件生成、自动改 Prompt、强化学习或失败后的自我进化。它只把“跑得是否正常、任务是否成功、失败在哪里、不同版本差多少”做扎实。

## 关键边界

DeepSeek Harness 使用官方极简模式：只有持久 Bash 和字符串编辑器，没有视觉、Skills 或 MCP。主版本固定为 `deepseek-harness-sdk==0.1.0rc7`，A/B 时显式切换到 rc6。

原始 Harness JSONL 永不改写，ATIF-v1.7 是统一分析格式。Verifier 决定 PASS/FAIL；没有 Verifier、用户反馈或人工结果的在线轨迹记为 UNKNOWN。Judge 只解释已知失败，不负责判定成功。

失败归因不是“两次都让 AI 猜”。代码先确定性提取工具错误、完全重复调用、文件编辑、测试执行与结果、缺少验证和超时信号；随后一次 Judge 阅读完整轨迹和证据包，可选择任意真实步骤，也可以在证据不足时 ABSTAIN。

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

Harbor 适配器位于独立工作区 `/Users/shaw/Desktop/harbor-dsh-evalplant`。下面的 smoke job 会运行一个真实容器，安装 rc7，调用 `deepseek-v4-flash` 创建文件，再由 Verifier 判分：

```bash
cd /Users/shaw/Desktop/harbor-dsh-evalplant
uv run harbor run \
  -c examples/configs/agents/dsh-minimal-job.yaml \
  --job-name evalplant-dsh-rc7-smoke \
  -y
```

把完整 Harbor job 导入 EvalPlant：

```bash
cd /Users/shaw/eval-plant
uv run evalplant --db data/evalplant-harbor.db import \
  /Users/shaw/Desktop/harbor-dsh-evalplant/jobs/evalplant-dsh-rc7-smoke \
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
