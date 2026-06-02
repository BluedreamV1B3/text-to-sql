# Text-to-SQL Benchmark Evaluation

This repository combines the project pieces that were previously split across:

- `C:\Jonathan`: preprocessing, schemas/questions, LLM SQL generation
- `C:\Users\Administrator\Text2SQLEval`: SQL Server execution and metric calculation

The code evaluates LLM-generated SQL for a Microsoft SQL Server text-to-SQL benchmark across BiomedSQL, Insurance, and KaggleDBQA-derived databases.

## Repository Layout

```text
generation/             LLM adapters and SQL-generation pipelines
evaluation/             SQL Server runtime execution, reruns, and metric summaries
BiomedSQL/              Biomed schema and question definitions
Insurance/              Insurance schema and question/query definitions
KaggleDBQA/             KaggleDBQA schemas, questions, and prompt examples
benchmark/workbooks/    Lightweight benchmark workbooks for batches 1-4
benchmark/results/      Aggregate and per-batch tabular result summaries
notebooks/evaluation/   Original evaluation notebooks kept for reference
docs/                   Setup and workflow notes
```

Large runtime JSON outputs, raw CSV data, SQL Server database files, and local experiment artifacts are intentionally excluded from git.

## Setup

1. Create and activate a Python environment.
2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` or set the variables directly in your shell.
4. Configure SQL Server databases and logins as described in [docs/sql_server_setup.md](docs/sql_server_setup.md).

## Typical Workflow

Generate SQL batches:

```powershell
python generation/run_three_batch_generations.py --base benchmark/workbooks/master_results_batch_1.xlsx --runs 2,3,4 --output-template artifacts/generation_outputs/master_results_batch_{run}.xlsx
```

Run SQL Server runtime submissions and rebuild summaries:

```powershell
python evaluation/orchestrate_batch_runtimes.py
```

Rerun the focused BiomedSQL missing-table fix after importing required CSV tables:

```powershell
python evaluation/import_biomed_missing_tables.py --csv-dir data/biomed_csv_files/done
python evaluation/rerun_biomed_missing_table_rows.py
```

See [docs/workflow.md](docs/workflow.md) for the fuller workflow, and
[docs/results_data.md](docs/results_data.md) for the included result-data layout.

## Safety Notes

- API keys must be supplied through environment variables only.
- Do not commit `.env`, raw data, SQL result JSONs, or full SQL Server database files.
- `notebooks/evaluation` is included for reference; generation notebooks with embedded historical credentials were intentionally excluded.
