import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd

import eval_runner
import generate_full_benchmark_summary as benchmark_summary
from eval_runner import DEFAULT_CONNECTION_STRINGS, run_eval


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIRST_BATCH_WORKBOOK = Path(
    os.environ.get(
        "TEXT2SQL_BATCH1_WORKBOOK",
        PROJECT_ROOT / "benchmark" / "workbooks" / "master_results_batch_1.xlsx",
    )
)
NEW_BATCHES = {
    2: Path(
        os.environ.get(
            "TEXT2SQL_BATCH2_WORKBOOK",
            PROJECT_ROOT / "benchmark" / "workbooks" / "master_results_batch_2.xlsx",
        )
    ),
    3: Path(
        os.environ.get(
            "TEXT2SQL_BATCH3_WORKBOOK",
            PROJECT_ROOT / "benchmark" / "workbooks" / "master_results_batch_3.xlsx",
        )
    ),
    4: Path(
        os.environ.get(
            "TEXT2SQL_BATCH4_WORKBOOK",
            PROJECT_ROOT / "benchmark" / "workbooks" / "master_results_batch_4.xlsx",
        )
    ),
}
RESULT_ROOT = Path(
    os.environ.get("TEXT2SQL_SQL_RESULT_ROOT", PROJECT_ROOT / "artifacts" / "sql_run_result_final")
)
FINAL_ROOT = Path(
    os.environ.get("TEXT2SQL_FINAL_RESULT_ROOT", PROJECT_ROOT / "artifacts" / "final_result_files")
)
_external_result_root = os.environ.get("BATCH_RESULT_STORAGE_ROOT")
EXTERNAL_RESULT_ROOT = Path(_external_result_root) if _external_result_root else RESULT_ROOT
MODELS = ["golden", "golden_new", "gpt4o", "claude", "gemini"]
RUN_MODEL_COLUMNS = {
    "golden_set": "golden",
    "sql_submit_gpt4o": "gpt4o",
    "sql_submit_claude": "claude",
    "sql_submit_gemini": "gemini",
}


class QuietTqdm:
    """Minimal tqdm-compatible wrapper for non-interactive runs."""

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


def read_key(workbook: Path) -> pd.DataFrame:
    df = pd.read_excel(workbook)
    required = ["index", "id", "db_id", "question"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"{workbook} is missing required columns: {missing}")
    key = df[required].copy()
    key["index"] = key["index"].astype(int)
    key["db_id"] = key["db_id"].astype(str).str.strip()
    key["question"] = key["question"].astype(str)
    return key.sort_values("index").reset_index(drop=True)


def validate_batches() -> pd.DataFrame:
    base = read_key(FIRST_BATCH_WORKBOOK)
    rows = []
    for batch, workbook in NEW_BATCHES.items():
        current = read_key(workbook)
        same_request_ids = base["index"].tolist() == current["index"].tolist()
        same_id = base[["index", "id"]].equals(current[["index", "id"]])
        same_db = base[["index", "db_id"]].equals(current[["index", "db_id"]])
        same_questions = base[["index", "question"]].equals(current[["index", "question"]])
        rows.append(
            {
                "batch": batch,
                "workbook": str(workbook),
                "row_count": len(current),
                "same_request_ids": same_request_ids,
                "same_id": same_id,
                "same_db_id": same_db,
                "same_questions": same_questions,
                "matches_first_batch": all(
                    [same_request_ids, same_id, same_db, same_questions]
                ),
            }
        )

        if not rows[-1]["matches_first_batch"]:
            merged = base.merge(
                current,
                on="index",
                how="outer",
                suffixes=("_batch_1", f"_batch_{batch}"),
                indicator=True,
            )
            bad = merged[
                (merged["_merge"] != "both")
                | (merged["id_batch_1"] != merged[f"id_batch_{batch}"])
                | (merged["db_id_batch_1"] != merged[f"db_id_batch_{batch}"])
                | (merged["question_batch_1"] != merged[f"question_batch_{batch}"])
            ]
            mismatch_path = FINAL_ROOT / f"batch_{batch}" / "validation_mismatches.csv"
            mismatch_path.parent.mkdir(parents=True, exist_ok=True)
            bad.to_csv(mismatch_path, index=False)

    validation = pd.DataFrame(rows)
    FINAL_ROOT.mkdir(parents=True, exist_ok=True)
    validation.to_csv(FINAL_ROOT / "batch_workbook_validation.csv", index=False)
    return validation


def run_command(args: list[str]) -> None:
    subprocess.run(args, check=True)


def ensure_junction(link_path: Path, target_path: Path) -> None:
    if link_path.resolve() == target_path.resolve():
        link_path.mkdir(parents=True, exist_ok=True)
        return
    target_path.mkdir(parents=True, exist_ok=True)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.exists():
        return
    run_command(["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)])


def active_request_rows(workbook: Path) -> pd.DataFrame:
    df = pd.read_excel(workbook)
    df["index"] = df["index"].astype(int)
    df["db_id"] = df["db_id"].astype(str).str.strip()
    return df[["index", "db_id"]].sort_values(["db_id", "index"]).reset_index(drop=True)


def hardlink_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def write_index_csv(db_dir: Path) -> None:
    rows = []
    for path in sorted(db_dir.glob("*.json"), key=lambda p: int(p.stem)):
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


def rebuild_indexes(batch_result_dir: Path, models: Iterable[str]) -> None:
    for model in models:
        model_dir = batch_result_dir / model
        if not model_dir.exists():
            continue
        for db_dir in model_dir.iterdir():
            if db_dir.is_dir():
                write_index_csv(db_dir)


def organize_first_batch() -> None:
    batch_dir = RESULT_ROOT / "batch_1"
    batch_final_dir = FINAL_ROOT / "batch_1"
    batch_dir.mkdir(parents=True, exist_ok=True)
    batch_final_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIRST_BATCH_WORKBOOK, batch_final_dir / "master_results.xlsx")

    active = active_request_rows(FIRST_BATCH_WORKBOOK)
    missing = []
    for model in MODELS:
        for row in active.itertuples(index=False):
            src = RESULT_ROOT / model / row.db_id / f"{row.index}.json"
            dst = batch_dir / model / row.db_id / f"{row.index}.json"
            if not src.exists():
                missing.append({"model": model, "db_id": row.db_id, "request_id": row.index})
                continue
            hardlink_or_copy(src, dst)

    if missing:
        pd.DataFrame(missing).to_csv(batch_final_dir / "missing_result_jsons.csv", index=False)
        raise FileNotFoundError(
            f"Batch 1 is missing {len(missing)} active result JSON files. "
            f"See {batch_final_dir / 'missing_result_jsons.csv'}"
        )

    rebuild_indexes(batch_dir, MODELS)
    build_batch_summary(1, FIRST_BATCH_WORKBOOK, batch_dir, batch_final_dir)


def mirror_golden_to_golden_new(batch_result_dir: Path) -> None:
    src_root = batch_result_dir / "golden"
    dst_root = batch_result_dir / "golden_new"
    for src in src_root.rglob("*.json"):
        dst = dst_root / src.relative_to(src_root)
        hardlink_or_copy(src, dst)
    rebuild_indexes(batch_result_dir, ["golden_new"])


def build_batch_summary(
    batch: int, workbook: Path, result_dir: Path, final_dir: Path
) -> None:
    final_dir.mkdir(parents=True, exist_ok=True)
    benchmark_summary.RESULT_DIR = result_dir
    benchmark_summary.MASTER_WORKBOOK = workbook
    benchmark_summary.OUT_XLSX = final_dir / "full_benchmark_results.xlsx"
    summary = benchmark_summary.build_summary()
    sheets = benchmark_summary.build_model_summaries(summary)
    benchmark_summary.write_excel(summary, sheets)
    summary.to_csv(final_dir / "full_benchmark_results.csv", index=False)
    sheets["accuracy_by_model"].assign(batch=batch).to_csv(
        final_dir / "accuracy_by_model.csv", index=False
    )
    sheets["accuracy_by_model_db"].assign(batch=batch).to_csv(
        final_dir / "accuracy_by_model_db.csv", index=False
    )
    sheets["execution_summary"].assign(batch=batch).to_csv(
        final_dir / "execution_summary.csv", index=False
    )


def run_new_batch(batch: int, workbook: Path) -> None:
    batch_result_dir = RESULT_ROOT / f"batch_{batch}"
    batch_storage_dir = EXTERNAL_RESULT_ROOT / f"batch_{batch}"
    batch_final_dir = FINAL_ROOT / f"batch_{batch}"

    ensure_junction(batch_result_dir, batch_storage_dir)
    batch_final_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(workbook, batch_final_dir / "master_results.xlsx")

    summary = run_eval(
        excel_path=str(workbook),
        connection_strings=DEFAULT_CONNECTION_STRINGS,
        base_output_dir=str(batch_result_dir),
        num_workers=4,
        overwrite_json=True,
        model_columns=RUN_MODEL_COLUMNS,
    )
    summary.to_csv(batch_final_dir / "sql_run_summary.csv", index=False)
    mirror_golden_to_golden_new(batch_result_dir)
    rebuild_indexes(batch_result_dir, MODELS)
    build_batch_summary(batch, workbook, batch_result_dir, batch_final_dir)


def count_jsons(batch_result_dir: Path) -> pd.DataFrame:
    rows = []
    for model in MODELS:
        model_dir = batch_result_dir / model
        if not model_dir.exists():
            continue
        for db_dir in sorted(p for p in model_dir.iterdir() if p.is_dir()):
            rows.append(
                {
                    "model": model,
                    "db_id": db_dir.name,
                    "json_count": len(list(db_dir.glob("*.json"))),
                    "index_csv_exists": (db_dir / "index.csv").exists(),
                }
            )
    return pd.DataFrame(rows)


def write_combined_outputs() -> None:
    inventory_rows = []
    accuracy_frames = []
    accuracy_db_frames = []
    execution_frames = []

    for batch in [1, 2, 3, 4]:
        result_dir = RESULT_ROOT / f"batch_{batch}"
        final_dir = FINAL_ROOT / f"batch_{batch}"
        workbook = final_dir / "master_results.xlsx"
        summary_xlsx = final_dir / "full_benchmark_results.xlsx"
        counts = count_jsons(result_dir)
        if not counts.empty:
            counts.to_csv(final_dir / "result_json_counts.csv", index=False)

        inventory_rows.append(
            {
                "batch": batch,
                "workbook": str(workbook),
                "result_dir": str(result_dir),
                "summary_workbook": str(summary_xlsx),
                "active_question_count": len(pd.read_excel(workbook)) if workbook.exists() else None,
                "json_count_total": int(counts["json_count"].sum()) if not counts.empty else 0,
            }
        )

        if summary_xlsx.exists():
            acc = pd.read_excel(summary_xlsx, sheet_name="accuracy_by_model")
            acc.insert(0, "batch", batch)
            accuracy_frames.append(acc)
            acc_db = pd.read_excel(summary_xlsx, sheet_name="accuracy_by_model_db")
            acc_db.insert(0, "batch", batch)
            accuracy_db_frames.append(acc_db)
            execution = pd.read_excel(summary_xlsx, sheet_name="execution_summary")
            execution.insert(0, "batch", batch)
            execution_frames.append(execution)

    inventory = pd.DataFrame(inventory_rows)
    inventory.to_csv(FINAL_ROOT / "all_batches_inventory.csv", index=False)

    with pd.ExcelWriter(FINAL_ROOT / "all_batches_summary.xlsx", engine="openpyxl") as writer:
        inventory.to_excel(writer, sheet_name="inventory", index=False)
        if accuracy_frames:
            pd.concat(accuracy_frames, ignore_index=True).to_excel(
                writer, sheet_name="accuracy_by_model", index=False
            )
        if accuracy_db_frames:
            pd.concat(accuracy_db_frames, ignore_index=True).to_excel(
                writer, sheet_name="accuracy_by_model_db", index=False
            )
        if execution_frames:
            pd.concat(execution_frames, ignore_index=True).to_excel(
                writer, sheet_name="execution_summary", index=False
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--skip-batch1", action="store_true")
    parser.add_argument("--batches", nargs="*", type=int, default=[2, 3, 4])
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    eval_runner.tqdm = QuietTqdm

    validation = validate_batches()
    print(validation.to_string(index=False))
    if not validation["matches_first_batch"].all():
        raise ValueError("At least one new batch workbook does not match the first batch.")
    if args.validate_only:
        return

    if not args.skip_batch1:
        print("\nOrganizing batch 1...")
        organize_first_batch()

    for batch in args.batches:
        if batch not in NEW_BATCHES:
            raise ValueError(f"Unknown batch: {batch}")
        print(f"\nRunning batch {batch} from {NEW_BATCHES[batch]}...")
        run_new_batch(batch, NEW_BATCHES[batch])

    write_combined_outputs()
    print(f"\nAll batch outputs are organized under {FINAL_ROOT.resolve()}")
    print(f"SQL result folders are organized under {RESULT_ROOT.resolve()}")


if __name__ == "__main__":
    main()
