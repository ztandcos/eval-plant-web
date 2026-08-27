# Harbor 接入方式

Harbor 是 EvalPlant 的执行后端。当前 `dsh-minimal` 依赖本目录保存的四个补丁；EvalPlant 会自动优先使用项目内已打补丁的 `harbor/.venv/bin/harbor`，也支持用 `EVALPLANT_HARBOR` 指向另一份已打补丁二进制。

## 用户路径

准备好已应用四个补丁的 Harbor（本机项目内已经完成）。之后用户只使用 EvalPlant：

```bash
evalplant bench --agent dsh --bench terminal-bench --sandbox docker --k 3
evalplant bench --agent dsh --agent claude-code --bench ./my-tasks --task hello-world
evalplant bench --print-config --agent dsh --bench terminal-bench
```

EvalPlant 生成 Harbor `JobConfig` JSON（Agent、dataset、environment、k、concurrency），内部执行 `harbor run -c … -y`，再导入轨迹并归因。密钥只写成 `${DEEPSEEK_API_KEY}` 这类模板，不进入进程参数。

## 补丁开发（可选）

`harbor-patches/` 相对 Harbor 基线 `b37833221e27435a18d7acdd41d875cdc2831893` 的四笔定制：注册 `dsh-minimal`、固定 DeepSeek Harness SDK `0.1.0rc7`、避免凭据进入进程参数、心跳与失败 attempt 归档。恢复 tree 指纹应为 `bfea9c800c913be0d23225b7f8472a3ac5f06f9e`。

```bash
git clone https://github.com/harbor-framework/harbor.git harbor
git -C harbor checkout b37833221e27435a18d7acdd41d875cdc2831893
git -C harbor am integrations/harbor-patches/*.patch
export EVALPLANT_HARBOR_ROOT="$PWD/harbor"
# 可选：export EVALPLANT_HARBOR="$PWD/harbor/.venv/bin/harbor"
```

补丁不含真实 API Key。EvalPlant 不反向控制运行中的容器；隔离和基础设施重试仍由 Harbor 负责。
