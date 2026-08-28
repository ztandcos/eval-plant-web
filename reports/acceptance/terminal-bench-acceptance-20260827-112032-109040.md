# Agent Evaluation: terminal-bench-acceptance

**Ship Gate: FAIL**

| Candidate | Metrics | Improved | Regressed | Unchanged | Cost | Latency | Gate |
|---|---|---:|---:|---:|---:|---:|---:|
| dsh-flash | pass@1 100.0% → 0.0% | 0 | 1 | 0 | n/a | 224.4% | FAIL |

原因：
- candidate pass@k drop exceeded threshold
- 1 new regression(s); limit is 0

## Regression triage

### Regression #1

- candidate: `dsh-flash`
- task: `terminal-bench@2.0/kv-store-grpc`
- triage: NEW_REGRESSION
- category: L4 Verification（反馈验证）
- root cause: 模型在后台启动服务器后未验证其持续运行，导致验证时端口未监听，任务失败。
- evidence: step 10

## Failure clusters

- L4 Verification（反馈验证）: 1

## Contract violations

- premature_termination: 1

## Recommended actions

主要回归来自 L4 Verification（反馈验证）。

- L4 Verification（反馈验证） (1): 增加反馈处理、完成前验证和终止条件。
