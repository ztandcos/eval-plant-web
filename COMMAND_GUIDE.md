# EvalPlant 完整运行教程

这份文档从一台刚拿到项目的 Mac 开始讲，主示例使用 Terminal-Bench 2.0 的真实任务 `kv-store-grpc`。正常使用时只需要记住 `evalplant bench`：你告诉 EvalPlant 用哪个 Agent、跑哪个 Bench、选哪道题和哪种沙盒，它会在内部调用仓库自带的 Harbor，任务一结束就运行 Verifier；通过的任务直接记为 PASS，失败的任务马上进入归因，最后写入 SQLite 并生成一份 JSON 总报告。

## 一、第一次使用前准备

本项目需要 Python 3.12、`uv` 和正在运行的 Docker Desktop。Python 3.12 是内置 Harbor 的要求；EvalPlant 自身虽然兼容更低版本，但为避免一台机器维护两套 Python，直接统一使用 3.12。

```bash
cd /Users/shaw/eval-plant

uv python install 3.12
uv sync --python 3.12
uv sync --project harbor --python 3.12 --no-dev
```

三条命令分别表示：准备 Python 3.12；安装 EvalPlant 自己的依赖；安装仓库内固定 Harbor 执行引擎的运行依赖。它们只需要在第一次克隆项目或依赖更新后执行，不是每次跑任务都执行。`--no-dev` 表示不安装 Harbor 那些很大的云沙盒和开发测试依赖，本地 Docker 评测不需要它们。

然后确认 Docker Desktop 已经打开：

```bash
docker info
```

只要命令能打印服务器信息，而不是提示无法连接，就说明容器环境可用。

### 安全地设置 DeepSeek Key

请使用一个没有在聊天、Git 或截图中公开过的新 Key。下面的写法不会把 Key 留在 shell 历史里：

```bash
read -s "DEEPSEEK_API_KEY?请输入新的 DeepSeek API Key: "
echo
export DEEPSEEK_API_KEY
```

这个 Key 同时供两个角色使用：`dsh` Agent 用 DeepSeek 做任务；任务失败时，Judge 也用 DeepSeek 分析轨迹。Key 只通过环境变量传递，不会被展开写进 Job JSON 或进程参数。关闭当前终端后变量自动失效。

## 二、亲手跑一个真实 Terminal-Bench 任务

先做一次不花钱的配置预览：

```bash
uv run evalplant bench \
  --agent dsh \
  --bench terminal-bench@2.0 \
  --task kv-store-grpc \
  --sandbox docker \
  --k 1 \
  --concurrency 1 \
  --experiment tbench-kv-store-grpc-preview \
  --print-config
```

这一步不会启动容器，也不会调用 DeepSeek。它只验证你选择的 Agent、数据集、任务、沙盒和次数能否组成合法配置，并在 `data/jobs/` 下写出一份配置 JSON。`terminal-bench@2.0` 是真实公开数据集，`kv-store-grpc` 是其中一题：Agent 需要在 Linux 容器中实现并启动一个符合要求的 gRPC 键值存储服务，Verifier 会从容器外检查协议、读写行为和服务存活状态。

确认配置后，真实运行：

```bash
uv run evalplant bench \
  --agent dsh \
  --bench terminal-bench@2.0 \
  --task kv-store-grpc \
  --sandbox docker \
  --k 1 \
  --concurrency 1 \
  --experiment tbench-kv-store-grpc-dsh \
  --agent-model deepseek/deepseek-v4-flash \
  --model deepseek-v4-pro \
  --output data/tbench-kv-store-grpc-dsh-report.json
```

这一个命令会完成整条链路：下载并固定真实题目，创建 Docker 容器，让 `dsh-minimal` 调用 `deepseek-v4-flash` 做题，运行 Terminal-Bench 自带 Verifier，持续记录生命周期、心跳、ATIF 轨迹、token、成本和耗时，把结果导入 `data/evalplant.db`。如果 Verifier 判定失败，系统会先运行确定性 Harness 规则；规则无法确认时才调用 `deepseek-v4-pro` Judge，并校验证据是否确实来自原轨迹。最后终端显示统计表，同时生成 `data/tbench-kv-store-grpc-dsh-report.json`。

`--agent-model` 和 `--model` 很容易混淆。前者是“干活的人”使用的模型，后者是“失败后看病的人”使用的 Judge 模型。任务通过时不会调用 Judge，因此不会产生 Judge 费用。任务失败但命中确定性规则时也不会调用 Judge。

### 你会看到什么

运行期间会先看到 Job 配置位置和正在运行的 Agent、Bench、沙盒。每个任务完成后会立即出现一行结果：

```text
OK    terminal-bench/kv-store-grpc  PASS
```

这表示 Verifier 已经确认任务做对，无需归因。失败时类似：

```text
FAIL  terminal-bench/kv-store-grpc  → ATTRIBUTED  LLM/L4  step=14
      report: .../tbench-kv-store-grpc-dsh-report.json
```

这表示任务没有通过，诊断认为主要责任是 LLM 的 L4“反馈验证与结束”，根因证据落在第 14 步。`ATTRIBUTED` 只表示结果通过了结构和证据来源校验，不代表语义因果一定等同人工判断；可信度仍要结合 evidence、counterfactual 和人工抽检查看。

最终有三类主要产物：

```text
data/evalplant.db
data/jobs/tbench-kv-store-grpc-dsh/
data/tbench-kv-store-grpc-dsh-report.json
```

数据库保存实验、Task、Trial、Outcome、Check、步骤索引和诊断。`jobs/` 保存 Harbor 原始作业目录，包括轨迹、Verifier 日志和生命周期事件。报告 JSON 是最适合给程序、网页后台或面试演示读取的汇总，顶层包含 `statistics`、`trials` 和 `diagnoses`：`statistics` 是总体指标，`trials` 是每次真实运行与检查项，`diagnoses` 只包含失败 Trial 的完整归因。

## 三、`bench` 主命令的所有参数

完整写法是：

```bash
uv run evalplant [--db 数据库路径] bench [参数]
```

`--db` 是全局参数，必须放在 `bench` 前面。默认是 `data/evalplant.db`。例如你想做一场完全隔离的面试演示，可以写：

```bash
uv run evalplant --db data/interview-demo.db bench ...
```

`bench` 的参数含义如下。

| 参数 | 默认值 | 大白话含义 | 会产生什么影响 |
|---|---:|---|---|
| `--agent NAME` | 必填 | 选干活的 Agent，例如 `dsh`、`claude-code`、`codex`、`oracle`。 | 可重复填写，重复后会让多个 Agent 跑同一批题。`dsh` 会解析成仓库内的 `dsh-minimal`。 |
| `--bench NAME_OR_PATH` | 必填 | 选远程 Bench 名或本地题目目录。 | `terminal-bench@2.0` 会从 Harbor 注册表解析固定版本；目录则直接读取你自己的题。可重复填写。 |
| `--task NAME_OR_GLOB` | 全部任务 | 只挑 Bench 中的某题或一组通配题。 | 示例是 `kv-store-grpc`。不写就会跑整个 Bench，可能非常慢且昂贵。可重复填写。 |
| `--sandbox TYPE` | `docker` | 任务在哪种隔离环境里执行。 | 本机用 `docker`；云沙盒还需要对应依赖和 Key。支持项用 `bench --list` 查看。 |
| `--k N` | `1` | 同一道题重复跑几次。 | `k=3` 会产生三个 Trial，用于看随机波动、pass@k 和稳定通过率。必须至少为 1。 |
| `--concurrency N` | `4` | 最多同时跑几个 Trial。 | 本机第一次建议 1；数字越大越吃 CPU、内存、Docker 和 API 并发额度。必须至少为 1。 |
| `--experiment NAME` | UTC 时间名 | 这批实验的唯一名字。 | 同时作为数据库实验 ID、Job 名和默认报告名。建议人工明确填写，方便后续查询和比较。 |
| `--jobs-dir PATH` | 数据库同目录下的 `jobs/` | 原始 Job 放在哪里。 | 只改变原始轨迹、日志和生命周期文件的位置，不改变 SQLite 路径。 |
| `--agent-model MODEL` | Agent 别名自己的默认值 | 干活 Agent 使用的模型。 | `dsh` 默认是 `deepseek/deepseek-v4-flash`；显式写出最便于复现。当前它会应用到本次选择的所有 Agent。 |
| `--agent-kwarg K=V` | 无 | 给 Agent 传额外配置。 | 可重复，例如版本或 Agent 专属开关；会应用到本次选择的所有 Agent。普通运行不需要。 |
| `--env NAME` | 自动识别常见模型 Key | 额外把当前终端中的某个环境变量安全注入容器。 | 配置里只写 `${NAME}`，不复制真实值。DeepSeek/OpenAI/Anthropic/Google 常见 Key 已自动识别。 |
| `--force-build` | 关闭 | 强制重建任务镜像。 | Dockerfile 或依赖变了、旧镜像缓存有问题时使用；会显著增加耗时。 |
| `--harbor PATH` | 仓库内 `harbor/.venv/bin/harbor` | 临时指定另一个兼容 Harbor 二进制。 | 只用于开发排障，正常用户不要写；业务命令不需要知道 Harbor 路径。 |
| `--print-config` | 关闭 | 只打印将要执行的配置。 | 写出 Job JSON 后退出，不启动容器、不调用模型、不生成实验结果。 |
| `--list` | 关闭 | 列出可用 Agent 别名和沙盒。 | 不运行任务。命令为 `uv run evalplant bench --list`。 |
| `--model MODEL` | `deepseek-v4-pro` 或环境变量 `EVALPLANT_JUDGE_MODEL` | 失败归因所用 Judge。 | 只在失败轨迹未被确定性规则直接判断时调用。 |
| `--max-input-tokens N` | `100000` | 单次 Judge 允许的最大估算输入。 | 短轨迹放完整内容；超出后转长轨迹分层视图；连索引都超限则记 `INPUT_TOO_LARGE`，不调用模型。 |
| `--force` | 关闭 | 强制覆盖已存在的诊断。 | 会再次调用需要 LLM 的 Judge，可能产生费用；默认复用已有诊断。 |
| `--gold PATH` | 无 | 可选人工金标 JSONL。 | 总报告后额外计算责任、类别、根因步骤和覆盖率；正常工程运行不需要。 |
| `--output PATH` | 数据库目录下的 `<experiment>-report.json` | 指定最终机器报告位置。 | 父目录不存在时自动创建。 |
| `--poll-seconds S` | `3.0` | 多久检查一次运行中新结果。 | 只影响状态刷新频率，不影响 Agent 行为。必须不小于 0。 |
| `--lost-after-seconds N` | `90` | 多久收不到心跳后显示为 LOST。 | 这是观测判断，不会擅自杀死或篡改任务结果。必须大于 0。 |

常见矩阵写法如下，它会让两个 Agent 分别跑两套 Bench，但 `--task` 会应用到每套 Bench，所以只有任务命名兼容时才这样写：

```bash
uv run evalplant bench \
  --agent dsh \
  --agent oracle \
  --bench terminal-bench@2.0 \
  --task kv-store-grpc \
  --k 3 \
  --concurrency 2 \
  --experiment tbench-agent-matrix
```

不同供应商 Agent 通常需要不同模型名，当前一个 `--agent-model` 会应用给所有 Agent，因此跨供应商比较最好拆成两个 experiment，再用 `compare` 配对，不要硬塞进一条命令。

## 四、其余命令：什么时候用、每个参数是什么

这些命令不会取代 `bench`。它们用于你已经有 Harbor Job、ATIF 轨迹或数据库实验时做复盘和排障。

### 查看版本和总帮助

```bash
uv run evalplant --version
uv run evalplant --help
uv run evalplant bench --help
```

`--version` 打印当前 EvalPlant 版本。`--help` 不改文件、不访问网络；放在某个子命令后就查看该命令参数。

### `run`：已有数据的一体化处理

```bash
uv run evalplant run PATH_OR_EXPERIMENT [参数]
```

它接收三种东西：轨迹目录、Harbor Job 目录、已经导入数据库的 experiment 名。目录里有 `execution-events.jsonl` 且任务还在运行时，它会持续观察，每条失败一落盘就诊断；普通目录则一次性导入、诊断和出报告。

```bash
uv run evalplant run data/jobs/tbench-kv-store-grpc-dsh \
  --experiment tbench-kv-replay \
  --model deepseek-v4-pro \
  --output data/tbench-kv-replay-report.json
```

`PATH_OR_EXPERIMENT` 是唯一位置参数。`--experiment` 指定写入哪个实验，省略时通常取目录名。`--model`、`--max-input-tokens`、`--force`、`--gold`、`--output`、`--poll-seconds` 和 `--lost-after-seconds` 与 `bench` 含义相同。`--once` 表示只处理当前已经存在的文件然后退出，不持续等新 Trial；复盘完整 Job 时建议加上。

### `diagnose`：`run --once` 的易读别名

```bash
uv run evalplant diagnose data/jobs/tbench-kv-store-grpc-dsh \
  --experiment tbench-kv-replay \
  --model deepseek-v4-pro
```

它和 `run` 的一次性模式完全相同，只是没有 `--once` 参数，因为这个命令天然只跑一遍。其余参数全部与 `run` 相同。名字叫 diagnose，不代表它忽略 PASS；它仍会先导入全部 Outcome，只对失败 Trial 做归因，然后出总报告。

### `import`：只入库，不归因

```bash
uv run evalplant import TRACE_PATH \
  --experiment imported-tbench \
  --agent-model deepseek/deepseek-v4-flash
```

`TRACE_PATH` 是 ATIF 文件、轨迹目录或 Harbor Job。`--experiment` 必填，决定写入哪个批次。`--agent-model` 是上游没有模型字段时的补充标记，不会调用这个模型。产物只有 SQLite 中的实验、Task、Trial、Outcome、Check 和步骤索引，不会调用 Judge，也不会自动导出 JSON 报告。

### `observe`：只看容器任务状态

```bash
uv run evalplant observe data/jobs/tbench-kv-store-grpc-dsh \
  --experiment tbench-kv-store-grpc-dsh \
  --lost-after-seconds 90 \
  --output data/tbench-kv-status.json
```

位置参数必须是 Harbor Job 目录。`--experiment` 必填，用来归属生命周期事件。`--lost-after-seconds` 是无心跳判 LOST 的秒数，默认 90。`--output` 可选，把终端状态表同时写成 JSON。这个命令只同步 START、阶段、HEARTBEAT、END、CANCEL 等事件，不做归因、不触发重试；真正的隔离和基础设施重试由内置 Harbor 执行层完成。

### `analyze`：只给已入库的失败轨迹归因

```bash
uv run evalplant analyze \
  --experiment imported-tbench \
  --model deepseek-v4-pro \
  --max-input-tokens 100000
```

`--experiment` 必填。`--model` 默认 `deepseek-v4-pro`。`--max-input-tokens` 默认 100000。`--trajectory ID` 只分析一个指定轨迹，适合先做单条付费冒烟；不写就处理该实验全部可诊断失败。`--force` 会覆盖已有诊断并可能重新付费。它不会重新跑 Agent 或 Verifier。

### `inspect`：把一条轨迹和诊断摊开看

```bash
uv run evalplant inspect TRAJECTORY_ID
```

它只有一个位置参数：数据库中的 trajectory ID。终端会显示 Agent、模型、数据集、Outcome、reward、责任、类别、摘要，以及按真实编号排列的步骤。`★` 是主根因步骤，`•` 是被报告引用的证据步骤。这个命令只读数据库。

如果不知道 ID，可以先打开报告 JSON，在 `trials[].trajectory_id` 或 `diagnoses[].trajectory_id` 中找到。

### `report`：不重跑，重新统计和导出

```bash
uv run evalplant report \
  --experiment tbench-kv-store-grpc-dsh \
  --output data/tbench-kv-store-grpc-dsh-report-copy.json
```

`--experiment` 必填。`--output` 可选；不写时只在终端显示，不创建新报告文件。它从 SQLite 重新统计 Task 数、Trial 数、通过率、Check、责任、类别、成本、token 和耗时，不调用 Agent、Verifier 或 Judge。

### `compare`：比较两个 Agent 或两个版本

先保证两个 experiment 跑的是相同任务。例如：

```bash
uv run evalplant compare \
  --baseline tbench-dsh-old \
  --candidate tbench-dsh-new \
  --k 3 \
  --max-cost-increase 0.20 \
  --output data/tbench-dsh-old-vs-new.json
```

`--baseline` 是旧版本实验，必填。`--candidate` 是候选新版本实验，必填。`--k` 默认 1，表示每个双方共有 Task 各取前几次 Trial；双方某题不足 k 次时，这题不进入比较。`--max-cost-increase` 默认 0.2，也就是允许平均 Agent 成本最多上涨 20%。`--output` 可选，保存机器可读比较结果。

输出包含经验 `pass@k`、`pass^k`、平均成本、Agent 耗时、改进题、回归题和 Ship Gate。只要出现共有任务回归、候选 pass@k 下降，或平均成本涨幅越过阈值，Gate 就是 FAIL。这里比较的是 Agent 做题成本，不是 Judge 诊断成本。

### 独立金标评测工具

这是开发期校准 Judge 用的模块，不是日常执行主链路：

```bash
uv run python -m evalplant.evaluation \
  --gold tests/eval_cases/gold.jsonl \
  --predictions reports/run-a.json reports/run-b.json \
  --reviews tests/eval_cases/evidence-reviews.jsonl
```

`--gold` 是人工金标 JSONL。`--predictions` 至少一份报告；传多份时还会计算同样本重复运行的一致率。`--reviews` 是可选人工证据语义审核。它输出覆盖率、责任/类别/根因步骤准确率、邻近步骤准确率、拒答数、证据支持率和稳定性，不修改数据库。

## 五、诊断状态到底是什么意思

任务结果和诊断结果是两件事。任务结果来自 Verifier：`PASS` 是做对，`FAIL` 是做错，`TIMEOUT` 是 Agent 超时，`INFRA_ERROR` 是执行基础设施异常，`UNKNOWN/INCOMPLETE` 是结果不足。

失败后才有诊断状态。`ATTRIBUTED` 表示已找到通过格式和来源校验的主要根因；`HARNESS_SUSPECTED` 表示怀疑执行框架但证据不足以精确落类；`UNDETERMINED` 表示系统诚实拒答；`INPUT_TOO_LARGE` 表示连长轨迹索引都超过配置预算且没有调用 Judge；`FAILED` 表示归因程序或 Judge 调用本身失败，它不会把原任务改成失败；`OUTCOME_ONLY` 是轨迹视图模式，表示 Agent 有 Verifier 结果但没有可供诊断的 ATIF，因此失败会落为 `UNDETERMINED / trajectory_unavailable`。

责任为 `HARNESS` 时使用 H-E 到 H-G 七层；责任为 `LLM` 时使用 L1 到 L4 四类。完整定义见 `DIAGNOSIS_SPEC.md`。SQLite 每张表和每个字段的含义见 `SQLITE_DATA_DICTIONARY.md`。

## 六、最常见问题

出现 `Judge returned an empty response`，说明任务执行结果已经保存，但 Judge 服务这次返回了空内容。系统会把诊断记为 `FAILED`，不会丢掉 Outcome。先检查 Key、模型名和 DeepSeek 服务状态，再用 `analyze --trajectory ID --force` 只重试这一条，不要重跑整个 Bench。

出现 “bundled Harbor environment is not installed”，执行：

```bash
uv sync --project harbor --python 3.12 --no-dev
```

出现 Docker 无法连接，打开 Docker Desktop，等左下角显示 Engine running，再执行 `docker info`。

第一次跑某道 Terminal-Bench 题会下载题目、拉取或构建镜像，并在容器中安装 DSH SDK，所以明显比第二次慢。`kv-store-grpc` 的成功或失败不是固定的：模型具有运行波动；通过就不会产生诊断，失败才会展示完整归因，这正是系统的真实行为。

## 七、开发者验收命令

正常使用不需要执行这些。修改 EvalPlant 后运行：

```bash
uv run python -m unittest discover -s tests -v
```

要修改内置 Harbor 时，先安装它的开发依赖，再运行项目实际依赖的定向测试：

```bash
uv sync --project harbor --python 3.12

harbor/.venv/bin/python -m pytest \
  harbor/tests/unit/agents/installed/test_dsh_minimal.py \
  harbor/tests/unit/test_job_status.py \
  harbor/tests/unit/test_trial_queue_integration.py
```

构建 EvalPlant 安装包：

```bash
uv build
```

产物进入 `dist/`。Harbor 是仓库内固定 fork，不再有独立 `.git`、补丁目录或需要人工重放的补丁；它的上游基线和内置改动记录在 `harbor/EVALPLANT_FORK.md`。
