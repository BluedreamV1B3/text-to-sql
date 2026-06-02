# Results Data

The repository includes publishable tabular result data under:

```text
benchmark/results/final_result_files/
```

This folder mirrors the important analysis outputs from the local `final_result_files` directory:

- per-batch full benchmark summaries
- per-batch model accuracy summaries
- per-batch database-level accuracy summaries
- execution summaries
- result JSON count inventories
- Biomed missing-table rerun inventories and validation files
- aggregate four-batch summary workbook

Raw SQL runtime JSON outputs from `sql_run_result_final/` are not committed. They are multi-GB artifacts, and at least one individual JSON file is around 2 GB. Use the included `result_json_counts.csv` files to verify runtime coverage, and regenerate raw JSON outputs locally with `evaluation/orchestrate_batch_runtimes.py` when needed.
