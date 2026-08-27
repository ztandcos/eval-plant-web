# Harbor + DeepSeek Harness 集成

根目录下的 `harbor/` 是本机工作副本，体积约 1.7GB，因此不直接提交进 EvalPlant。为了让交付可复现，`harbor-patches/` 保存了相对于 Harbor 基线 `b37833221e27435a18d7acdd41d875cdc2831893` 的四笔定制提交。

在另一台机器上恢复相同集成：

```bash
git clone https://github.com/harbor-framework/harbor.git harbor
git -C harbor checkout b37833221e27435a18d7acdd41d875cdc2831893
git -C harbor am ../integrations/harbor-patches/*.patch
```

恢复后的 Git tree 指纹应为 `bfea9c800c913be0d23225b7f8472a3ac5f06f9e`；`git am` 会生成新的提交 ID，所以不要求 HEAD 与本机提交 ID 相同。四笔补丁依次完成：注册 `dsh-minimal` Agent、固定 DeepSeek Harness SDK `0.1.0rc7` 并稳定安装流程、避免把凭据暴露在进程参数中、增加任务心跳与状态事件并保留隔离的失败重试记录。

运行 Harbor 自带示例：

```bash
cd harbor
export DEEPSEEK_API_KEY='你的有效 Key'
uv run harbor run --config examples/configs/agents/dsh-minimal-job.yaml
```

Harbor 生成 job 后，回到 EvalPlant 根目录，使用 `evalplant run harbor/jobs/JOB_NAME` 导入并诊断；作业仍在跑时同一条命令会监视失败 trial。补丁中不包含任何真实 API Key。

运行期间可在另一个终端同步查看状态：

```bash
uv run evalplant observe harbor/jobs/JOB_NAME --experiment JOB_NAME
```

Harbor 自己负责并发隔离和基础设施重试；EvalPlant 只消费 `execution-events.jsonl`，不会反向控制运行中的容器。
