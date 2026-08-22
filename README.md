# EvalPlant

EvalPlant 是一个面向 Coding Agent 的离线失败归因平台。它使用 BugsInPy
提供可复现的真实缺陷，让 mini-SWE-agent 完整执行修复任务；任务失败后，再由
DeepSeek Judge 定位轨迹中最早的关键错误，并与人工标注进行比较。

当前版本已经打通以下流程：

```text
BugsInPy 任务准备 → 隔离运行 Agent → 保存轨迹与测试结果
                                      ↓
人工标注 ← 指标评估 ← DeepSeek 失败归因
```

## 安装

本机需要安装 Docker 和 uv。安装项目依赖后可以查看所有命令：

```bash
uv sync
uv run evalplant --help
```

运行 Agent 或 Judge 前，需要在终端设置 DeepSeek API Key。不要把 Key 写入
代码、镜像或 Git：

```bash
export DEEPSEEK_API_KEY="你的新 API Key"
```

mini-SWE-agent 默认使用 `deepseek/deepseek-v4-flash`，归因 Judge 默认使用
`deepseek-v4-pro`，都可以通过命令行参数覆盖。

## 一、准备 Benchmark

BugsInPy 只需要下载一次：

```bash
git clone https://github.com/soarsmu/BugsInPy.git .benchmarks/BugsInPy
```

固定 Agent 镜像也只需要构建一次。镜像包含 EvalPlant 执行器、
mini-SWE-agent、uv、Git 和基础 Python 运行时，不包含 benchmark、具体任务、
数据库、Oracle 数据或其他任务环境：

```bash
docker build --platform linux/amd64 -t evalplant-agent:0.2 .
```

## 二、准备一个任务

下面的命令只准备 `fastapi-5`，并把任务源码及其 Python 环境写入独立目录：

```bash
uv run evalplant docker-prepare \
  --bugsinpy-root .benchmarks/BugsInPy \
  --project fastapi \
  --bug 5 \
  --workspace .workspaces/fastapi-5 \
  --oracle-dir data/oracle
```

可信的准备容器会看到三个挂载点：BugsInPy 位于 `/bench`，当前任务位于
`/task`，Oracle 输出位于 `/oracle`。BugsInPy 的 checkout 脚本需要临时修改
benchmark 目录，因此准备阶段的 `/bench` 是可写的；真正运行 Agent 时不会挂载
`/bench` 或 `/oracle`。

准备程序会确认缺陷版本测试失败、修复版本测试通过，然后删除原始 Git 历史和
修复提交信息。任务虚拟环境直接构建在容器路径 `/task`，因此以后无论宿主机目录
位于哪里，都可以稳定挂载到新的 Agent 容器中。

## 三、运行一个 Agent 任务

一个容器只运行一个任务。下面的命令把当前任务动态挂载到 `/task`，把本次产物
目录挂载到 `/output`：

```bash
uv run evalplant docker-run .workspaces/fastapi-5 \
  --experiment fastapi-5-flash \
  --run-dir data/raw/fastapi-5-flash \
  --step-limit 20
```

Agent 容器使用只读根文件系统，只能修改当前 `/task` 和 `/output`。它看不到其他
任务目录、BugsInPy、Oracle、EvalPlant 源码和宿主机数据库。容器结束后，宿主机
会把结果导入 SQLite。

`--step-limit 20` 表示最多允许 20 次 Agent 决策循环；设为 `0` 表示使用
mini-SWE-agent 的默认限制。当前版本只实现单任务执行和隔离边界，尚未加入并发
队列或容器调度。

每次运行会产生这些主要文件：

```text
trajectory.traj.json  Agent 原始轨迹
agent.log              Agent 运行日志
final.patch            Agent 最终代码改动
baseline_test.log      修改前的失败测试
final_test.log         修改后的验证测试
verdict.json           PASS、FAIL、TIMEOUT 或 INFRA_ERROR
```

## 四、分析失败并归因

归因在宿主机执行，只分析 `FAIL` 或 `TIMEOUT` 轨迹：

```bash
uv run evalplant analyze --experiment fastapi-5-flash
```

Judge 采用双层归因。第一层从完整轨迹中筛选候选错误步骤，第二层结合前后文确定
最早的关键错误、失败阶段、失败机制、证据步骤和置信度。结果会缓存到 SQLite；
需要重新分析时使用 `--force`。

在终端查看某条轨迹及归因结果：

```bash
uv run evalplant inspect TRAJECTORY_ID
```

## 五、人工标注与评估

只需要给失败样本做人工标注：

```bash
uv run evalplant annotate TRAJECTORY_ID \
  --split test \
  --step 12 \
  --stage fault_localization \
  --mechanism wrong_assumption \
  --evidence 10,12,14 \
  --oracle-used
```

生成归因准确率、阶段和机制 Macro-F1、证据通过率及归因覆盖率：

```bash
uv run evalplant report --experiment fastapi-5-flash --split test
```

已有的 mini-SWE-agent 运行目录也可以直接导入：

```bash
uv run evalplant import /path/to/run --experiment exp-001
```

原始轨迹和日志保存在本地文件系统中；SQLite 只保存文件路径、哈希、标准化步骤
摘要、Judge 归因和人工标注。
