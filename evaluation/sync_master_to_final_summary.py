import json
import shutil
from pathlib import Path

import pandas as pd

from eval_runner import _parse_submit


MASTER_PATH = Path("final_result_files/master_results_gold_corrected.xlsx")
SUMMARY_PATH = Path("final_result_files/full_benchmark_results_summary_gold_corrected.xlsx")
BACKUP_PATH = Path("final_result_files/old/master_results_gold_corrected_before_sync_to_final_summary.xlsx")

SUBMIT_COLUMNS = ["golden_set", "sql_submit_gpt4o", "sql_submit_claude", "sql_submit_gemini"]


def submit_cell(sql: str, metadata: dict) -> str:
    return f"runner.submit({json.dumps(sql, ensure_ascii=False)}, metadata={json.dumps(metadata, ensure_ascii=False)})"


def main() -> None:
    if not MASTER_PATH.exists():
        raise FileNotFoundError(MASTER_PATH)
    if not SUMMARY_PATH.exists():
        raise FileNotFoundError(SUMMARY_PATH)

    BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MASTER_PATH, BACKUP_PATH)

    master = pd.read_excel(MASTER_PATH)
    summary = pd.read_excel(SUMMARY_PATH, sheet_name="full_benchmark_summary")
    summary_unique = summary.drop_duplicates("request_id").copy()
    summary_unique["request_id"] = summary_unique["request_id"].astype(int)

    summary_by_id = summary_unique.set_index("request_id", drop=False)
    keep_ids = summary_unique["request_id"].tolist()

    master = master.copy()
    master["index"] = master["index"].astype(int)
    before_rows = len(master)

    filtered = master[master["index"].isin(keep_ids)].copy()
    filtered["_order"] = filtered["index"].map({request_id: i for i, request_id in enumerate(keep_ids)})
    filtered = filtered.sort_values("_order").drop(columns=["_order"]).reset_index(drop=True)

    missing = sorted(set(keep_ids) - set(filtered["index"].astype(int)))
    if missing:
        raise ValueError(f"Summary request IDs missing from master workbook: {missing}")

    dropped = sorted(set(master["index"].astype(int)) - set(keep_ids))

    for idx, row in filtered.iterrows():
        request_id = int(row["index"])
        srow = summary_by_id.loc[request_id]

        filtered.at[idx, "question"] = srow["question"]
        filtered.at[idx, "query_ground_truth"] = srow["golden_sql"]

        # Rebuild golden_set so the SQL, question, and request_id mirror the
        # final summary exactly.
        _, metadata = _parse_submit(str(row["golden_set"]))
        metadata["request_id"] = request_id
        metadata["model"] = "golden"
        metadata["db_id"] = str(row["db_id"])
        metadata["question"] = str(srow["question"])
        filtered.at[idx, "golden_set"] = submit_cell(str(srow["golden_sql"]), metadata)

    # Validate runtime request_id values in all submission columns.
    mismatches = []
    for _, row in filtered.iterrows():
        request_id = int(row["index"])
        for column in SUBMIT_COLUMNS:
            _, metadata = _parse_submit(str(row[column]))
            if int(metadata.get("request_id")) != request_id:
                mismatches.append((request_id, column, metadata.get("request_id")))

    if mismatches:
        raise ValueError(f"Runtime request_id mismatches: {mismatches[:10]}")

    filtered.to_excel(MASTER_PATH, index=False)

    print(f"Updated {MASTER_PATH.resolve()}")
    print(f"Backup saved to {BACKUP_PATH.resolve()}")
    print(f"Rows: {before_rows} -> {len(filtered)}")
    print(f"Dropped request IDs ({len(dropped)}): {dropped}")
    print(f"Distinct questions: {filtered['question'].nunique()}")
    print("Runtime request_id validation passed for golden/gpt4o/claude/gemini submit columns.")


if __name__ == "__main__":
    main()
