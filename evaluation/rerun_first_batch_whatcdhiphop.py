import csv
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

import eval_runner
from eval_runner import DEFAULT_CONNECTION_STRINGS, run_eval


MASTER_WORKBOOK = Path("final_result_files/master_results.xlsx")
OUTPUT_DIR = Path("sql_run_result_final")
WORK_DIR = Path("final_result_files/whatcdhiphop_first_batch_rerun")
RERUN_WORKBOOK = WORK_DIR / "whatcdhiphop_first_batch_rerun.xlsx"
SUMMARY_CSV = Path("final_result_files/sql_run_result_final_golden_corrected_summary.csv")
SUMMARY_CSV_BACKUP = Path("final_result_files/old/sql_run_result_final_golden_corrected_summary_before_whatcdhiphop_rerun.csv")

MODEL_COLUMNS = {
    "golden_set": "golden",
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
    subset = master[master["db_id"].astype(str).eq("WhatCDHipHop")].copy()
    subset = subset.sort_values("index").reset_index(drop=True)
    if subset.empty:
        raise ValueError("No WhatCDHipHop rows found in final first-batch master workbook.")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    subset.to_excel(RERUN_WORKBOOK, index=False)
    return subset


def mirror_golden_to_golden_new() -> None:
    src_dir = OUTPUT_DIR / "golden" / "WhatCDHipHop"
    dst_dir = OUTPUT_DIR / "golden_new" / "WhatCDHipHop"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src in src_dir.glob("*.json"):
        shutil.copy2(src, dst_dir / src.name)


def rebuild_index_csv(folder: str) -> None:
    db_dir = OUTPUT_DIR / folder / "WhatCDHipHop"
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
    print(f"WhatCDHipHop rows: {len(subset)}")

    eval_runner.tqdm = QuietTqdm
    summary = run_eval(
        excel_path=str(RERUN_WORKBOOK),
        connection_strings=DEFAULT_CONNECTION_STRINGS,
        base_output_dir=str(OUTPUT_DIR),
        num_workers=4,
        overwrite_json=True,
        model_columns=MODEL_COLUMNS,
    )

    SUMMARY_CSV_BACKUP.parent.mkdir(parents=True, exist_ok=True)
    if SUMMARY_CSV.exists():
        shutil.copy2(SUMMARY_CSV, SUMMARY_CSV_BACKUP)
    summary.to_csv(SUMMARY_CSV, index=False)

    mirror_golden_to_golden_new()
    for folder in ["golden", "golden_new", "gpt4o", "claude", "gemini"]:
        rebuild_index_csv(folder)

    print(f"Rerun summary saved to: {SUMMARY_CSV.resolve()}")
    print(f"Previous CSV backup: {SUMMARY_CSV_BACKUP.resolve()}")
    print("Mirrored golden to golden_new and rebuilt WhatCDHipHop index.csv files.")


if __name__ == "__main__":
    main()
