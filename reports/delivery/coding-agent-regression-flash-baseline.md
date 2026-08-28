# Terminal-Bench flash baseline

**This is a baseline snapshot, not a Ship Gate.** Candidate `dsh-pro` was not run. The suite run `coding-agent-regression-20260827-143242-944110` remains `RUNNING` so `evalplant resume` can still start pro later.

Experiment: `coding-agent-regression-20260827-143242-944110-dsh-flash`
Agent: Harbor `dsh-minimal` 0.1.0rc7 · Model: `deepseek/deepseek-v4-flash` · k=3 · 12 Terminal-Bench 2.0 tasks
Registered as production baseline for suite `coding-agent-regression` (`dsh-flash`).

## Why this run exists

EvalPlant decides ship/no-ship by comparing a candidate to a production baseline on the same tasks. Self-built evals already have a flash vs pro Gate. This run pins the harder public distribution so later agent/model/prompt changes have a real before/after.

## Headline

| Metric | Value |
|---|---|
| Tasks | 12 |
| Trials | 36 |
| Trial pass rate | 50% (18/36) |
| Task any-pass (≈ pass@3) | 67% (8/12) |
| Task all-pass (pass^3) | 33% (4/12) |
| Weighted check pass rate | 80% |
| Verdicts | PASS 18 · FAIL 11 · INFRA_ERROR 6 · TIMEOUT 1 |
| Mean agent time | 293s |
| Mean cost | n/a (provider did not return `cost_usd`) |

6/36 trials never reached the agent: Docker address-pool exhaustion (`filter-js-from-html`, `openssl-selfsigned-cert`, `regex-log`, `sanitize-git-repo` one trial each) or apt install failures (`filter-js-from-html`, `kv-store-grpc` one trial each). Treat those as harness noise. They pull the headline pass rate down; they are not evidence that flash cannot do those tasks.

## Per task

| Task | Trials | Any pass | All pass | Notes |
|---|---:|:---:|:---:|---|
| `cancel-async-tasks` | 3 | yes | no | FAIL, FAIL, PASS |
| `configure-git-webserver` | 3 | no | no | TIMEOUT, FAIL, FAIL |
| `db-wal-recovery` | 3 | no | no | FAIL, FAIL, FAIL |
| `filter-js-from-html` | 3 | no | no | FAIL, INFRA, INFRA |
| `fix-git` | 3 | yes | yes | PASS, PASS, PASS |
| `kv-store-grpc` | 3 | no | no | INFRA, FAIL, FAIL |
| `log-summary-date-ranges` | 3 | yes | yes | PASS, PASS, PASS |
| `nginx-request-logging` | 3 | yes | yes | PASS, PASS, PASS |
| `openssl-selfsigned-cert` | 3 | yes | no | INFRA, PASS, PASS |
| `regex-log` | 3 | yes | no | PASS, PASS, INFRA |
| `sanitize-git-repo` | 3 | yes | no | FAIL, PASS, INFRA |
| `sqlite-db-truncate` | 3 | yes | yes | PASS, PASS, PASS |

Stable full passes: `fix-git`, `nginx-request-logging`, `log-summary-date-ranges`, `sqlite-db-truncate`.

Never passed: `kv-store-grpc` (server not up at verifier), `configure-git-webserver` (wrong plan or timeout), `filter-js-from-html` (1 real fail + 2 infra), `db-wal-recovery` (guessed records instead of decrypting WAL).

## What this does **not** claim

- No flash vs pro delta, no Ship Gate PASS/FAIL.
- Cost gate cannot fire until the provider reports cost.
- Diagnosis accuracy is a separate report; do not treat Judge labels as ground truth. See `reports/delivery/diagnosis-accuracy.md` (32 human gold cases: category 41%, exact root step 18%).

## Resume candidate later

```bash
uv run evalplant --db data/delivery.db resume coding-agent-regression-20260827-143242-944110
```

Generated 2026-08-28T02:19:18Z.
