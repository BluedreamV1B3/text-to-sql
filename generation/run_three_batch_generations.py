from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path

import pandas as pd

from master_results_sqlserver_pipeline import (
    MODEL_SQL_COLUMNS,
    MODEL_SUBMIT_COLUMNS,
    generate_model_sql,
    parse_models,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = PROJECT_ROOT / "benchmark" / "workbooks" / "master_results_batch_1.xlsx"
DEFAULT_OUTPUT_TEMPLATE = str(
    PROJECT_ROOT / "artifacts" / "generation_outputs" / "master_results_batch_{run}.xlsx"
)
GENERATION_COLUMNS = list(MODEL_SQL_COLUMNS.values()) + list(MODEL_SUBMIT_COLUMNS.values())


def parse_runs(raw: str) -> list[int]:
    runs = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not runs:
        raise ValueError("At least one run number is required.")
    return runs


def require_env_vars(models: list[str]) -> None:
    required = []
    if "gpt4o" in models:
        required.append("OPENAI_API_KEY")
    if "claude" in models:
        required.append("ANTHROPIC_API_KEY")
    if "gemini" in models:
        required.append("GOOGLE_API_KEY")

    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")


def prepare_batch_workbook(base_path: Path, output_path: Path, reset_existing: bool) -> pd.DataFrame:
    if output_path.exists() and not reset_existing:
        return pd.read_excel(output_path)

    df = pd.read_excel(base_path)
    for col in GENERATION_COLUMNS:
        if col not in df.columns:
            df[col] = ""
        else:
            df[col] = ""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(output_path, index=False)
    return df


def verify_batch(path: Path, run: int, models: list[str]) -> list[tuple]:
    df = pd.read_excel(path)
    errors = []

    for model in models:
        sql_col = MODEL_SQL_COLUMNS[model]
        submit_col = MODEL_SUBMIT_COLUMNS[model]
        for _, row in df.iterrows():
            sql = "" if pd.isna(row[sql_col]) else str(row[sql_col])
            submit = "" if pd.isna(row[submit_col]) else str(row[submit_col])
            request_id = int(row["index"])

            if not sql.strip() or not submit.strip():
                errors.append((request_id, model, "missing_sql_or_submit"))
                continue

            try:
                call = ast.parse(submit, mode="eval").body
                sql_arg = ast.literal_eval(call.args[0])
                metadata = ast.literal_eval(call.keywords[0].value)
            except Exception as exc:
                errors.append((request_id, model, "parse", str(exc)))
                continue

            if sql_arg != sql:
                errors.append((request_id, model, "sql_mismatch"))
            if metadata.get("run") != run:
                errors.append((request_id, model, "run_mismatch"))
            if metadata.get("model") != model:
                errors.append((request_id, model, "model_mismatch"))
            if metadata.get("request_id") != request_id:
                errors.append((request_id, model, "request_id_mismatch"))
            if metadata.get("question") != row["question"]:
                errors.append((request_id, model, "question_mismatch"))

    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Run additional full-benchmark generation batches.")
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--runs", default="2,3,4")
    parser.add_argument("--models", default="gpt4o,claude,gemini")
    parser.add_argument("--output-template", default=DEFAULT_OUTPUT_TEMPLATE)
    parser.add_argument("--checkpoint-frequency", type=int, default=10)
    parser.add_argument("--reset-existing", action="store_true")
    args = parser.parse_args()

    runs = parse_runs(args.runs)
    models = parse_models(args.models)
    require_env_vars(models)

    if not args.base.exists():
        raise FileNotFoundError(args.base)

    for run in runs:
        output_path = Path(args.output_template.format(run=run))
        print("=" * 80)
        print(f"Batch run {run}: {output_path}")
        print("=" * 80)

        df = prepare_batch_workbook(args.base, output_path, args.reset_existing)
        df = generate_model_sql(
            df=df,
            output_path=output_path,
            models=models,
            run=run,
            skip_existing=True,
            row_start=None,
            row_end=None,
            row_limit=None,
            sample_per_db=None,
            sample_seed=2026,
            checkpoint_frequency=args.checkpoint_frequency,
        )

        errors = verify_batch(output_path, run, models)
        for col in GENERATION_COLUMNS:
            filled = int(df[col].fillna("").astype(str).str.strip().ne("").sum())
            print(f"{col}: {filled}/{len(df)}")
        print(f"verification_errors: {len(errors)}")
        if errors:
            print(errors[:20])
            raise RuntimeError(f"Batch run {run} failed verification.")

    print("All requested batch generations are complete.")


if __name__ == "__main__":
    main()
