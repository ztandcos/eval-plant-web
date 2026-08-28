# EvalPlant 完整评测平台目标

EvalPlant 的主链路是：变更触发评测套件，自动执行 Baseline 与 Candidate，在相同 Task 上完成多次 Trial，用 Verifier 和 CTRF 测试确定性判分，计算 pass@k、成本和延迟，只把新回归交给诊断器，最后生成 JSON/Markdown 回归报告和 Ship Gate。

当前实现目标已经收口为一条命令：

```bash
uv run evalplant eval suites/coding-agent-regression.yaml
```

Suite 负责声明版本、Benchmark、任务范围、重复次数、并发、指标和门禁。SQLite 保存 Suite Run 状态与生产 Baseline；第一次运行建立 Baseline，之后自动复用，候选通过后由明确的 promote 操作更新生产基线。中断后使用 `evalplant resume RUN_ID`，内部复用 Harbor 原生 job resume，不重跑已完成 Trial。

平台验收标准是：任务测试进入 Check；Baseline/Candidate 只比较共有任务；Infra 异常由 Harbor 重试；Baseline PASS、Candidate FAIL 才进入新回归诊断；报告能给出任务、证据、失败聚类、成本变化和最终门禁；PR、定时任务和 Release 可以调用同一个 Suite 命令。

当前阶段不建设 Web UI、消息队列、Kubernetes 或多租户数据库。达到多团队并发写入和长期在线服务需求后，再把 SQLite 与本地报告替换为 PostgreSQL、对象存储和权限层。

已落地的最小完整闭环：

1. `evalplant eval smoke` 解析 Suite 名，展开 Oracle Baseline/Candidate。
2. Harbor 在 Docker 中执行本地 `evals/smoke-file`，Verifier 不联网。
3. 结果进入 SQLite；第一次运行登记 production baseline，之后自动复用。
4. 失败按 INFRA_ERROR / KNOWN_FAILURE / NEW_REGRESSION 分诊，只诊断新回归。
5. JSON/Markdown 报告给出指标变化、聚类、整改建议和 Ship Gate；Gate 失败时退出码为 1。
6. `evalplant resume RUN_ID` 从中断处继续；GitHub Actions 用同一条命令做 PR 冒烟、Nightly 和 Release。
