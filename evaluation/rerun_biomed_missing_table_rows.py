import argparse
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

import eval_runner
from eval_runner import DEFAULT_CONNECTION_STRINGS, run_eval
from orchestrate_batch_runtimes import (
    MODELS,
    NEW_BATCHES,
    RESULT_ROOT,
    FINAL_ROOT,
    RUN_MODEL_COLUMNS,
    QuietTqdm,
    build_batch_summary,
    rebuild_indexes,
    write_combined_outputs,
)


MISSING_TABLES = [
    "DrugTargets_IndicationsAndTherapeuticUses",
    "AlzheimerDisease_CombinedGeneData_UUID",
]
RERUN_ROOT = FINAL_ROOT / "biomed_missing_table_rerun"


def affected_request_ids(batch: int) -> list[int]:
    summary_path = FINAL_ROOT / f"batch_{batch}" / "full_benchmark_results.xlsx"
    df = pd.read_excel(summary_path, sheet_name="full_benchmark_summary")
    errors = df["golden_error"].fillna("").astype(str)
    mask = (
        df["db_id"].astype(str).str.strip().eq("BiomedSQL")
        & errors.apply(lambda value: any(table in value for table in MISSING_TABLES))
    )
    return sorted(df.loc[mask, "request_id"].astype(int).unique().tolist())


def build_subset_workbook(batch: int, request_ids: list[int]) -> Path:
    workbook = NEW_BATCHES[batch]
    df = pd.read_excel(workbook)
    df["index"] = df["index"].astype(int)
    subset = df[df["index"].isin(request_ids)].copy()
    subset = subset.sort_values("index").reset_index(drop=True)
    if len(subset) != len(request_ids):
        found = set(subset["index"].astype(int).tolist())
        missing = sorted(set(request_ids) - found)
        raise ValueError(f"Batch {batch} workbook is missing affected request IDs: {missing}")
    if not subset["db_id"].astype(str).str.strip().eq("BiomedSQL").all():
        raise ValueError(f"Batch {batch} affected subset contains non-BiomedSQL rows")

    out_dir = RERUN_ROOT / f"batch_{batch}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "biomed_missing_table_subset.xlsx"
    subset.to_excel(out_path, index=False)
    (out_dir / "request_ids.json").write_text(
        json.dumps(request_ids, indent=2), encoding="utf-8"
    )
    return out_path


def sync_golden_new(batch: int, request_ids: list[int]) -> None:
    batch_dir = RESULT_ROOT / f"batch_{batch}"
    src_dir = batch_dir / "golden" / "BiomedSQL"
    dst_dir = batch_dir / "golden_new" / "BiomedSQL"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for request_id in request_ids:
        src = src_dir / f"{request_id}.json"
        dst = dst_dir / f"{request_id}.json"
        if not src.exists():
            raise FileNotFoundError(src)
        try:
            if dst.exists() and src.samefile(dst):
                continue
        except OSError:
            pass
        shutil.copy2(src, dst)


def rerun_batch(batch: int) -> dict:
    request_ids = affected_request_ids(batch)
    if not request_ids:
        print(f"Batch {batch}: no affected request IDs found; skipping rerun")
        return {
            "batch": batch,
            "affected_request_count": 0,
            "submitted": 0,
            "written": 0,
            "parse_errors": 0,
            "failed_slices": 0,
        }

    subset_workbook = build_subset_workbook(batch, request_ids)
    batch_result_dir = RESULT_ROOT / f"batch_{batch}"
    batch_final_dir = FINAL_ROOT / f"batch_{batch}"

    print(f"Batch {batch}: rerunning {len(request_ids)} BiomedSQL request IDs")
    summary = run_eval(
        excel_path=str(subset_workbook),
        connection_strings=DEFAULT_CONNECTION_STRINGS,
        base_output_dir=str(batch_result_dir),
        num_workers=4,
        overwrite_json=True,
        model_columns=RUN_MODEL_COLUMNS,
    )
    summary_path = RERUN_ROOT / f"batch_{batch}" / "sql_run_summary.csv"
    summary.to_csv(summary_path, index=False)

    sync_golden_new(batch, request_ids)
    rebuild_indexes(batch_result_dir, MODELS)
    build_batch_summary(
        batch,
        batch_final_dir / "master_results.xlsx",
        batch_result_dir,
        batch_final_dir,
    )

    failed = summary["last_error"].fillna("").astype(str).ne("").sum()
    return {
        "batch": batch,
        "affected_request_count": len(request_ids),
        "submitted": int(summary["submitted"].sum()),
        "written": int(summary["written"].sum()),
        "parse_errors": int(summary["parse_errors"].sum()),
        "failed_slices": int(failed),
    }


def refresh_error_reports() -> None:
    golden_rows = []
    model_rows = []
    for batch in [2, 3, 4]:
        path = FINAL_ROOT / f"batch_{batch}" / "full_benchmark_results.xlsx"
        df = pd.read_excel(path, sheet_name="full_benchmark_summary")

        ge = df[df["golden_error"].notna()][
            ["request_id", "source_id", "db_id", "question", "golden_error", "golden_sql"]
        ].drop_duplicates()
        if not ge.empty:
            ge = ge.copy()
            ge.insert(0, "batch", batch)
            golden_rows.append(ge)

        me = df[df["model_error"].notna()][
            ["request_id", "source_id", "db_id", "question", "model", "model_error", "model_sql"]
        ].copy()
        if not me.empty:
            me.insert(0, "batch", batch)
            model_rows.append(me)

    golden_path = FINAL_ROOT / "new_batches_golden_runtime_errors.csv"
    model_path = FINAL_ROOT / "new_batches_model_runtime_errors.csv"
    invalid_object_path = FINAL_ROOT / "new_batches_model_invalid_object_errors.csv"

    if golden_rows:
        pd.concat(golden_rows, ignore_index=True).to_csv(golden_path, index=False)
    else:
        pd.DataFrame(
            columns=[
                "batch",
                "request_id",
                "source_id",
                "db_id",
                "question",
                "golden_error",
                "golden_sql",
            ]
        ).to_csv(golden_path, index=False)

    if model_rows:
        all_model_errors = pd.concat(model_rows, ignore_index=True)
        all_model_errors.to_csv(model_path, index=False)
        invalid = all_model_errors[
            all_model_errors["model_error"]
            .fillna("")
            .astype(str)
            .str.contains("Invalid object name", regex=False)
        ]
        invalid.to_csv(invalid_object_path, index=False)
    else:
        pd.DataFrame().to_csv(model_path, index=False)
        pd.DataFrame().to_csv(invalid_object_path, index=False)


def validate_after() -> pd.DataFrame:
    rows = []
    for batch in [2, 3, 4]:
        df = pd.read_excel(
            FINAL_ROOT / f"batch_{batch}" / "full_benchmark_results.xlsx",
            sheet_name="full_benchmark_summary",
        )
        biomed = df[df["db_id"].astype(str).str.strip().eq("BiomedSQL")]
        missing_table_golden = biomed["golden_error"].fillna("").astype(str).apply(
            lambda value: any(table in value for table in MISSING_TABLES)
        )
        missing_table_model = biomed["model_error"].fillna("").astype(str).apply(
            lambda value: any(table in value for table in MISSING_TABLES)
        )
        acc = pd.read_excel(
            FINAL_ROOT / f"batch_{batch}" / "full_benchmark_results.xlsx",
            sheet_name="accuracy_by_model",
        )
        rows.append(
            {
                "batch": batch,
                "summary_rows": len(df),
                "unique_request_ids": int(df["request_id"].nunique()),
                "golden_errors_total": int(df["golden_error"].notna().sum()),
                "model_errors_total": int(df["model_error"].notna().sum()),
                "missing_table_golden_error_rows": int(missing_table_golden.sum()),
                "missing_table_model_error_rows": int(missing_table_model.sum()),
                "scored_gpt4o": int(acc.loc[acc["model"].eq("gpt4o"), "scored_query_count"].iloc[0]),
                "scored_claude": int(acc.loc[acc["model"].eq("claude"), "scored_query_count"].iloc[0]),
                "scored_gemini": int(acc.loc[acc["model"].eq("gemini"), "scored_query_count"].iloc[0]),
            }
        )
    out = pd.DataFrame(rows)
    out.to_csv(RERUN_ROOT / "post_rerun_validation.csv", index=False)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batches", nargs="*", type=int, default=[2, 3, 4])
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    eval_runner.tqdm = QuietTqdm
    RERUN_ROOT.mkdir(parents=True, exist_ok=True)

    results = [rerun_batch(batch) for batch in args.batches]
    pd.DataFrame(results).to_csv(RERUN_ROOT / "rerun_inventory.csv", index=False)
    write_combined_outputs()
    refresh_error_reports()
    validation = validate_after()
    print("\nRerun inventory:")
    print(pd.DataFrame(results).to_string(index=False))
    print("\nPost-rerun validation:")
    print(validation.to_string(index=False))


if __name__ == "__main__":
    main()
