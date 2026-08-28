# Agent Evaluation: evalplant-smoke-regression

**Ship Gate: FAIL**

| Candidate | Metrics | Improved | Regressed | Unchanged | Cost | Latency | Gate |
|---|---|---:|---:|---:|---:|---:|---:|
| nop | pass@1 100.0% → 0.0% | 0 | 1 | 0 | n/a | -100.0% | FAIL |

原因：
- candidate pass@k drop exceeded threshold
- 1 new regression(s); limit is 0

## Regression triage

### Regression #1

- candidate: `nop`
- task: `evals/evalplant/smoke-file`
- triage: NEW_REGRESSION
- category: UNDETERMINED
- root cause: Verifier 已确认任务失败，但 Agent 没有产出可供归因的轨迹。
- evidence: n/a

## Failure clusters

- UNDETERMINED: 1
