import shutil
from pathlib import Path

import pandas as pd
from openpyxl.styles import Font, PatternFill


WORKBOOK_PATH = Path("final_result_files/full_benchmark_results_summary_gold_corrected.xlsx")
SKIPPED_CSV = Path("final_result_files/neuro_only_rerun/skipped_deleted_drugtargets_pairs.csv")
BACKUP_PATH = Path("final_result_files/old/full_benchmark_results_summary_gold_corrected_before_drop_skipped.xlsx")


def build_model_summaries(summary: pd.DataFrame) -> dict[str, pd.DataFrame]:
    scored = summary[summary["actual_output_accuracy"].notna()].copy()

    model_summary = scored.groupby("model", as_index=False).agg(
        scored_query_count=("actual_output_accuracy", "count"),
        actual_output_accuracy_rate=("actual_output_accuracy", "mean"),
        row_count_match_rate=("actual_row_count_match", "mean"),
        value_hash_match_rate=("value_hash_match", "mean"),
        avg_runtime_delta_ms=("runtime_delta_ms", "mean"),
        median_runtime_delta_ms=("runtime_delta_ms", "median"),
    )

    not_scorable = (
        summary[summary["actual_output_accuracy"].isna()]
        .groupby("model")
        .size()
        .rename("not_scorable_count")
    )
    model_summary = model_summary.merge(not_scorable, on="model", how="outer").fillna({"not_scorable_count": 0})
    model_summary["not_scorable_count"] = model_summary["not_scorable_count"].astype(int)

    model_db_summary = scored.groupby(["model", "db_id"], as_index=False).agg(
        scored_query_count=("actual_output_accuracy", "count"),
        actual_output_accuracy_rate=("actual_output_accuracy", "mean"),
        row_count_match_rate=("actual_row_count_match", "mean"),
        value_hash_match_rate=("value_hash_match", "mean"),
        avg_runtime_delta_ms=("runtime_delta_ms", "mean"),
        median_runtime_delta_ms=("runtime_delta_ms", "median"),
    )

    execution_summary = summary.groupby("model", as_index=False).agg(
        total_query_count=("request_id", "count"),
        model_success_count=("model_ok", "sum"),
        golden_success_count=("golden_ok", "sum"),
        runtime_available_count=("runtime_missing", lambda values: int((values == 0.0).sum())),
    )
    execution_summary["model_success_rate"] = execution_summary["model_success_count"] / execution_summary["total_query_count"]
    execution_summary["golden_success_rate"] = execution_summary["golden_success_count"] / execution_summary["total_query_count"]

    issues = summary[
        (summary["actual_output_accuracy"].isna())
        | (summary["actual_output_accuracy"] == 0)
        | (~summary["model_ok"].astype(bool))
        | (~summary["golden_ok"].astype(bool))
    ].copy()

    return {
        "accuracy_by_model": model_summary,
        "accuracy_by_model_db": model_db_summary,
        "execution_summary": execution_summary,
        "issues": issues,
    }


def write_excel(summary: pd.DataFrame, sheets: dict[str, pd.DataFrame]) -> None:
    with pd.ExcelWriter(WORKBOOK_PATH, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="full_benchmark_summary", index=False)
        for sheet_name, sheet_df in sheets.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name[:31], index=False)

        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for cell in ws[1]:
                cell.font = Font(bold=True)
                cell.fill = PatternFill(fill_type="solid", fgColor="D9EAF7")

            for cell in ws[1]:
                header = str(cell.value or "")
                letter = cell.column_letter
                if header == "question":
                    width = 70
                elif header in {"actual_output_reason", "model_error", "golden_error"}:
                    width = 55
                elif header in {"model_result_rows", "golden_result_rows", "model_sql", "golden_sql"}:
                    width = 80
                elif header.endswith("_hash"):
                    width = 32
                elif header.endswith("_path"):
                    width = 45
                else:
                    width = min(max(len(header) + 2, 12), 28)
                ws.column_dimensions[letter].width = width

            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    cell.alignment = cell.alignment.copy(wrap_text=True, vertical="top")


def main() -> None:
    if not WORKBOOK_PATH.exists():
        raise FileNotFoundError(WORKBOOK_PATH)
    if not SKIPPED_CSV.exists():
        raise FileNotFoundError(SKIPPED_CSV)

    BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WORKBOOK_PATH, BACKUP_PATH)

    skipped = pd.read_csv(SKIPPED_CSV)
    request_ids = set(skipped["request_id"].astype(int))

    summary = pd.read_excel(WORKBOOK_PATH, sheet_name="full_benchmark_summary")
    before_rows = len(summary)
    before_requests = summary["request_id"].nunique()

    filtered = summary[~summary["request_id"].astype(int).isin(request_ids)].copy()
    filtered = filtered.reset_index(drop=True)

    sheets = build_model_summaries(filtered)
    write_excel(filtered, sheets)

    print(f"Updated {WORKBOOK_PATH.resolve()}")
    print(f"Backup saved to {BACKUP_PATH.resolve()}")
    print(f"Dropped request IDs ({len(request_ids)}): {sorted(request_ids)}")
    print(f"Rows: {before_rows} -> {len(filtered)}")
    print(f"Unique requests: {before_requests} -> {filtered['request_id'].nunique()}")
    print(sheets["accuracy_by_model"].to_string(index=False))


if __name__ == "__main__":
    main()
