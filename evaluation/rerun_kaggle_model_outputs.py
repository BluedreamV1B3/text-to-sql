import csv
import json
import sys
from pathlib import Path

import pandas as pd

import eval_runner
from eval_runner import DEFAULT_CONNECTION_STRINGS, run_eval


MASTER_WORKBOOK = Path("final_result_files/master_results_gold_corrected.xlsx")
OUTPUT_DIR = Path("sql_run_result_final")
RERUN_DIR = Path("final_result_files/kaggle_model_rerun")
RERUN_WORKBOOK = RERUN_DIR / "kaggle_model_rerun.xlsx"
RERUN_SUMMARY_CSV = RERUN_DIR / "kaggle_model_rerun_summary.csv"

KAGGLE_DBS = [
    "GeoNuclearData",
    "GreaterManchesterCrime",
    "StudentMathScore",
    "TheHistoryofBaseball",
    "WhatCDHipHop",
    "WorldSoccerDataBase",
]

MODEL_COLUMNS = {
    "sql_submit_gpt4o": "gpt4o",
    "sql_submit_claude": "claude",
    "sql_submit_gemini": "gemini",
}


class QuietTqdm:
    """Minimal tqdm-compatible wrapper for non-interactive reruns."""

    def __init__(self, iterable=None, **kwargs):
        self.iterable = iterable if iterable is not None else []

    def __iter__(self):
        return iter(self.iterable)

    def set_description(self, *args, **kwargs):
        return None

    def set_postfix(self, *args, **kwargs):
        return None

    def close(self):
        return None

    def write(self, msg):
        print(msg)


def build_rerun_workbook() -> pd.DataFrame:
    master = pd.read_excel(MASTER_WORKBOOK)
    subset = master[master["db_id"].astype(str).isin(KAGGLE_DBS)].copy()
    subset = subset.sort_values(["db_id", "index"]).reset_index(drop=True)
    if subset.empty:
        raise ValueError("No KaggleDBQA rows found in final master workbook.")

    RERUN_DIR.mkdir(parents=True, exist_ok=True)
    subset.to_excel(RERUN_WORKBOOK, index=False)
    return subset


def rebuild_index_csv(folder: str, db_id: str) -> None:
    db_dir = OUTPUT_DIR / folder / db_id
    if not db_dir.exists():
        return

    rows = []
    for path in sorted(db_dir.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else p.stem):
        with path.open("r", encoding="utf-8") as f:
            rec = json.load(f)
        observed = rec.get("observed") or {}
        metadata = rec.get("metadata") or {}
        rows.append(
            {
                "request_id": rec.get("request_id") or metadata.get("request_id"),
                "target_server_elapsed_ms": metadata.get("target_server_elapsed_ms"),
                "target_server_cpu_ms": metadata.get("target_server_cpu_ms"),
                "observed_server_elapsed_ms": observed.get("server_elapsed_ms"),
                "observed_server_cpu_ms": observed.get("server_cpu_ms"),
                "row_count": observed.get("row_count"),
                "ok": observed.get("ok"),
                "run_id": rec.get("run_id"),
                "job_id": rec.get("job_id"),
                "sql": rec.get("sql"),
                "golden_json_path": str(path),
            }
        )

    with (db_dir / "index.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "request_id",
                "target_server_elapsed_ms",
                "target_server_cpu_ms",
                "observed_server_elapsed_ms",
                "observed_server_cpu_ms",
                "row_count",
                "ok",
                "run_id",
                "job_id",
                "sql",
                "golden_json_path",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    subset = build_rerun_workbook()
    print(f"Created rerun workbook: {RERUN_WORKBOOK.resolve()}")
    print(f"Kaggle rows: {len(subset)}")
    print(subset["db_id"].value_counts().to_string())

    eval_runner.tqdm = QuietTqdm
    summary = run_eval(
        excel_path=str(RERUN_WORKBOOK),
        connection_strings=DEFAULT_CONNECTION_STRINGS,
        base_output_dir=str(OUTPUT_DIR),
        num_workers=4,
        overwrite_json=True,
        model_columns=MODEL_COLUMNS,
    )
    summary.to_csv(RERUN_SUMMARY_CSV, index=False)

    for model in MODEL_COLUMNS.values():
        for db_id in KAGGLE_DBS:
            rebuild_index_csv(model, db_id)

    print(f"Rerun summary saved to: {RERUN_SUMMARY_CSV.resolve()}")
    print("Rebuilt Kaggle model index.csv files.")


if __name__ == "__main__":
    main()
