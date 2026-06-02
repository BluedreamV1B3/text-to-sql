import json
from pathlib import Path

import pandas as pd

from eval_runner import _parse_submit


MASTER_PATH = Path("master_results_final.xlsx")
INSPECTION_PATH = Path("golden_query_inspection.xlsx")
OUTPUT_PATH = Path("master_results_gold_corrected.xlsx")


def _clean_sql(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _submit_cell(sql: str, metadata: dict) -> str:
    sql_literal = json.dumps(sql, ensure_ascii=False)
    metadata_literal = json.dumps(metadata, ensure_ascii=False)
    return f"runner.submit({sql_literal}, metadata={metadata_literal})"


def main() -> None:
    master = pd.read_excel(MASTER_PATH)
    inspection = pd.read_excel(INSPECTION_PATH)

    required_master_cols = {"index", "question", "query_ground_truth", "golden_set"}
    required_inspection_cols = {"request_id", "original_gold_sql", "new_gold_sql"}
    missing_master = required_master_cols - set(master.columns)
    missing_inspection = required_inspection_cols - set(inspection.columns)
    if missing_master:
        raise ValueError(f"Missing columns in {MASTER_PATH}: {sorted(missing_master)}")
    if missing_inspection:
        raise ValueError(f"Missing columns in {INSPECTION_PATH}: {sorted(missing_inspection)}")

    master = master.copy()
    master["index"] = master["index"].astype(int)
    inspection = inspection.copy()
    inspection["request_id"] = inspection["request_id"].astype(int)

    if len(master) != len(inspection):
        raise ValueError(f"Row count mismatch: master={len(master)} inspection={len(inspection)}")
    if set(master["index"]) != set(inspection["request_id"]):
        raise ValueError("The master index values do not match inspection request_id values.")

    inspection_by_id = inspection.set_index("request_id", drop=False)
    corrected_count = 0
    fallback_count = 0
    golden_cells = []
    corrected_sqls = []

    for _, row in master.iterrows():
        request_id = int(row["index"])
        review = inspection_by_id.loc[request_id]

        new_gold_sql = _clean_sql(review.get("new_gold_sql"))
        if new_gold_sql:
            corrected_sql = new_gold_sql
            corrected_count += 1
        else:
            corrected_sql = _clean_sql(review.get("original_gold_sql"))
            fallback_count += 1

        if not corrected_sql:
            raise ValueError(f"No corrected or original gold SQL available for request_id={request_id}")

        _, metadata = _parse_submit(str(row["golden_set"]))
        metadata["model"] = "golden"
        metadata["request_id"] = request_id
        metadata["question"] = str(row["question"])

        corrected_sqls.append(corrected_sql)
        golden_cells.append(_submit_cell(corrected_sql, metadata))

    master["query_ground_truth"] = corrected_sqls
    master["golden_set"] = golden_cells

    # Verify every generated submission can be parsed and still has matching request_id.
    for _, row in master.iterrows():
        _, metadata = _parse_submit(str(row["golden_set"]))
        if int(metadata["request_id"]) != int(row["index"]):
            raise ValueError(f"request_id mismatch after generation for index={row['index']}")

    master.to_excel(OUTPUT_PATH, index=False)

    print(f"Created {OUTPUT_PATH.resolve()}")
    print(f"Rows written: {len(master)}")
    print(f"Rows using nonblank new_gold_sql: {corrected_count}")
    print(f"Rows falling back to original_gold_sql: {fallback_count}")


if __name__ == "__main__":
    main()
