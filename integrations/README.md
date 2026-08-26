# Harbor + DeepSeek Harness 集成

根目录下的 `harbor/` 是本机工作副本，体积约 1.7GB，因此不直接提交进 EvalPlant。为了让交付可复现，`harbor-patches/` 保存了相对于 Harbor 基线 `b37833221e27435a18d7acdd41d875cdc2831893` 的三笔定制提交。

在另一台机器上恢复相同集成：

```bash
git clone https://github.com/harbor-framework/harbor.git harbor
git -C harbor checkout b37833221e27435a18d7acdd41d875cdc2831893
git -C harbor am ../integrations/harbor-patches/*.patch
```

恢复后的 Git tree 指纹应为 `71fedeeb4086ad858599fd825eba4465a44c8303`；`git am` 会生成新的提交 ID，所以不要求 HEAD 与本机提交 ID 相同。这三笔补丁依次完成：注册 `dsh-minimal` Agent、固定 DeepSeek Harness SDK `0.1.0rc7` 并稳定安装流程、避免把凭据暴露在进程参数中。

运行 Harbor 自带示例：

```bash
cd harbor
export DEEPSEEK_API_KEY='你的有效 Key'
uv run harbor run --config examples/configs/agents/dsh-minimal-job.yaml
```

Harbor 生成 job 后，回到 EvalPlant 根目录，使用 `evalplant import` 导入整个 job 目录。补丁中不包含任何真实 API Key。
