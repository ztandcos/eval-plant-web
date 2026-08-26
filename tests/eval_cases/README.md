# Diagnosis evaluation cases

Human-reviewed cases are local evaluation data, not runtime fixtures. Store one JSON object per line in `gold.jsonl` with `case_id`, `responsibility`, `category_code`, `root_cause_step`, `annotator` and `notes`. Do not derive these labels from the Judge being evaluated.

Evidence support is prediction-specific, so record it separately in `evidence-reviews.jsonl` as `{"case_id":"...","evidence_supported":true,"annotator":"...","notes":"..."}` after the reviewer reads the Judge output and raw source.

Run:

```bash
uv run python -m evalplant.evaluation \
  --gold tests/eval_cases/gold.jsonl \
  --predictions reports/run-a.json reports/run-b.json \
  --reviews tests/eval_cases/evidence-reviews.jsonl
```

The output separates coverage from selective accuracy so abstention cannot inflate the headline score, and reports repeated-run exact agreement. Public raw artifacts remain under ignored `data/`. Tracebench source annotations are useful review hints but are not automatically mapped to EvalPlant's Harness/LLM taxonomy.
