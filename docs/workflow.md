# Workflow

## 1. Generate SQL

The generation code lives in `generation/`. It loads schemas/questions from the shared repository folders and calls configured LLM providers through environment-variable credentials.

Common command:

```powershell
python generation/run_three_batch_generations.py --base benchmark/workbooks/master_results_batch_1.xlsx --runs 2,3,4 --output-template artifacts/generation_outputs/master_results_batch_{run}.xlsx
```

The generated workbooks contain raw model SQL plus `runner.submit(...)` runtime submission strings for SQL Server.

## 2. Execute SQL Runtime

The SQL Server runtime/evaluation code lives in `evaluation/`.

```powershell
python evaluation/orchestrate_batch_runtimes.py
```

This runs golden queries and model-generated queries, stores JSON outputs, mirrors golden outputs where needed, and builds per-batch summaries.

Runtime output folders are intentionally gitignored because they can be multi-GB.

## 3. Metric Calculation

`evaluation/generate_full_benchmark_summary.py` compares model result rows to golden result rows.

The current value comparison ignores:

- column names
- column order
- row order

It compares row values after canonicalization. A model output is marked correct when the value hash matches the golden value hash.

## 4. Gold Query Inspection

`evaluation/golden_query_inspection.py` supports schema-aware review of question/gold-SQL pairs and can produce corrected gold SQL.

Keep corrected benchmark workbooks in `benchmark/workbooks/` or a local artifact folder depending on whether they should be versioned.

## 5. Focused Reruns

Several maintenance scripts support targeted reruns without overwriting unrelated results:

- `evaluation/rerun_kaggle_model_outputs.py`
- `evaluation/rerun_biomed_missing_table_rows.py`
- `evaluation/rerun_first_batch_whatcdhiphop.py`
- `evaluation/rerun_neurodegenerative_rows.py`

Use these when SQL Server data or corrected SQL changes only affect a subset of rows.
