# Diagnosis accuracy (honest)

This is **not** a Ship Gate metric. EvalPlant’s product claim is suite orchestration, pairing, and a publish gate. Diagnosis is an auxiliary pass over failures. The numbers below are human-reviewed and are **not** high.

Gold: `tests/eval_cases/gold.jsonl` (32 cases, annotator drbz)  
Reviews: `tests/eval_cases/evidence-reviews.jsonl` (32)  
Predictions: `reports/delivery/diagnoses.predictions.json`  
Judge: `deepseek-v4-pro`, prompt `engineering_diagnosis_v3`, rules `harness_rules_v2`

Sources: custom 10-task flash/pro failures, plus Terminal-Bench flash failures from `coding-agent-regression-20260827-143242-944110-dsh-flash`.

## Scores

| Metric | Value | Read as |
|---|---:|---|
| Gold cases | 32 | All paired to a stored diagnosis |
| Coverage (status=ATTRIBUTED) | **84.4%** (27/32) | 4 Judge crashes (invalid evidence quote) + 1 timeout with no trajectory |
| Responsibility accuracy | **84.4%** | 100% on the 27 attributed cases |
| Category accuracy | **40.6%** | 48.1% if you ignore abstentions |
| Root-step exact | **18.2%** (4/22 labeled steps) | 22.2% selective |
| Root-step ±1 | **22.7%** | 27.8% selective |
| Evidence actually supports the claim | **46.9%** (15/32) | Quote may be real and still not support the causal claim |
| Cross-run stability | n/a | Only one prediction set |

Public RootSE reference in this repo remains **16/102 exact earliest step**. This 32-case set is the same order of magnitude. Do not write “automatic root-cause localization” on a resume.

## What the model actually gets wrong

1. **L4 instead of L3 on daemons.** Heartbeat and HTTP-echo agents start a process, verify it in-session, then the process is gone when the hidden verifier runs. Gold is L3 (tool/session persistence). Judge repeatedly says L4 “missing verification / completion signal.” The task has no completion token.
2. **L3 instead of L2 on wrong programs.** `cancel-async-tasks` cleanup and `filter-js-from-html` sanitizer are implementation/algorithm errors. Judge often points at a tool-edit step.
3. **Quote validation crashes.** Four trials (`heartbeat-worker__3CX3k2A`, `db-wal-recovery__dj9iKeF`, `configure-git-webserver__bVBHJbY`, `kv-store-grpc__NPaNQVm`) returned `FAILED` because a cited quote was not in the claimed step. That is a contract working as designed, not a success.
4. **Infra without ATIF used to come back UNDETERMINED.** Harbor `result.json` with `exception_info` and no trajectory is `harbor-result-v1`. The pipeline now runs harness rules on `INFRA_ERROR` (Docker compose, address pools, apt-get) and labels **H-E**. Docker build logs that contain the word “context” previously matched H-C; Docker/apt patterns are checked first.

## What is in scope to claim

- Failures are split into harness vs model before a person reads logs.
- Explicit infra exceptions can be attributed without calling the Judge.
- A person can audit quotes against step text.
- Category and exact step are **not** reliable enough to drive agent patches unsupervised.

Generated from `python -m evalplant.evaluation` on this gold/prediction/review triple.
