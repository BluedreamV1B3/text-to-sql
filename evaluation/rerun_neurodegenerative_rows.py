import csv
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

import eval_runner
from eval_runner import DEFAULT_CONNECTION_STRINGS, run_eval


ERROR_NEEDLE = "Invalid object name 'NeurodegenerativeDiseases_SMR_Genes_Full'"
MASTER_WORKBOOK = (
    Path("master_results_gold_corrected.xlsx")
    if Path("master_results_gold_corrected.xlsx").exists()
    else Path("final_result_files/master_results_gold_corrected.xlsx")
)
SUMMARY_WORKBOOK = Path("final_result_files/full_benchmark_results_summary_gold_corrected.xlsx")
OUTPUT_DIR = Path("sql_run_result_final")
RERUN_WORKBOOK = Path("final_result_files/rerun_neurodegenerative_rows.xlsx")
RERUN_SUMMARY_CSV = Path("final_result_files/rerun_neurodegenerative_rows_summary.csv")

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


def affected_request_ids() -> list[int]:
    summary = pd.read_excel(SUMMARY_WORKBOOK, sheet_name="full_benchmark_summary")
    mask = (
        summary["golden_error"].fillna("").astype(str).str.contains(ERROR_NEEDLE, regex=False)
        | summary["model_error"].fillna("").astype(str).str.contains(ERROR_NEEDLE, regex=False)
    )
    return sorted(summary.loc[mask, "request_id"].astype(int).unique().tolist())


def build_rerun_workbook(request_ids: list[int]) -> None:
    master = pd.read_excel(MASTER_WORKBOOK)
    master["index"] = master["index"].astype(int)
    subset = master[master["index"].isin(request_ids)].copy()
    subset = subset.sort_values("index").reset_index(drop=True)

    missing = sorted(set(request_ids) - set(subset["index"].astype(int)))
    if missing:
        raise ValueError(f"Request IDs missing from {MASTER_WORKBOOK}: {missing}")

    RERUN_WORKBOOK.parent.mkdir(parents=True, exist_ok=True)
    subset.to_excel(RERUN_WORKBOOK, index=False)
    print(f"Created rerun workbook: {RERUN_WORKBOOK.resolve()} ({len(subset)} rows)")


def mirror_golden_to_golden_new(request_ids: list[int]) -> None:
    for db_dir in (OUTPUT_DIR / "golden").iterdir():
        if not db_dir.is_dir():
            continue
        target_dir = OUTPUT_DIR / "golden_new" / db_dir.name
        target_dir.mkdir(parents=True, exist_ok=True)
        for request_id in request_ids:
            src = db_dir / f"{request_id}.json"
            if src.exists():
                shutil.copy2(src, target_dir / src.name)


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

    index_path = db_dir / "index.csv"
    with index_path.open("w", newline="", encoding="utf-8") as f:
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

    request_ids = affected_request_ids()
    if not request_ids:
        print(f"No rows found with error: {ERROR_NEEDLE}")
        return

    print(f"Affected request IDs ({len(request_ids)}): {request_ids}")
    build_rerun_workbook(request_ids)

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
    print(f"Rerun summary saved to: {RERUN_SUMMARY_CSV.resolve()}")

    mirror_golden_to_golden_new(request_ids)
    for folder in ["golden", "golden_new", "gpt4o", "claude", "gemini"]:
        rebuild_index_csv(folder, "BiomedSQL")
    print("Updated golden_new mirror and rebuilt BiomedSQL index.csv files.")


if __name__ == "__main__":
    main()
