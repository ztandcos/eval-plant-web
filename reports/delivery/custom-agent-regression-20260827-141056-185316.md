# Agent Evaluation: custom-agent-regression

**Ship Gate: PASS**

| Candidate | Metrics | Improved | Regressed | Unchanged | Cost | Latency | Gate |
|---|---|---:|---:|---:|---:|---:|---:|
| dsh-pro | pass@1 50.0% → 80.0%, pass@3 90.0% → 80.0% | 3 | 0 | 7 | n/a | 19.9% | PASS |

## Regression triage

No new regressions.

## Failure clusters

- L4 Verification（反馈验证）: 3
- L3 Tool Use（工具使用）: 2
- FAILED: 1

## Contract violations

- missing_verification_signal: 1
- missing_verification: 1
- tool_argument_schema: 1
- premature_termination: 1
- tool_usage_error: 1

## Recommended actions

主要回归来自 L4 Verification（反馈验证）。

- L4 Verification（反馈验证） (3): 增加反馈处理、完成前验证和终止条件。
- L3 Tool Use（工具使用） (2): 改进工具描述、参数 Schema 和 tool-use 行为。
