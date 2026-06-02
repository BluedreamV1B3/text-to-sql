import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


WORKBOOK_PATH = Path(
    os.environ.get(
        "RESULT_WORKBOOK_PATH",
        "final_result_files/full_benchmark_results_summary_gold_corrected.xlsx",
    )
)
if not WORKBOOK_PATH.exists() and Path("final_result_files/full_benchmark_results.xlsx").exists():
    WORKBOOK_PATH = Path("final_result_files/full_benchmark_results.xlsx")
RESULT_DIR = Path("sql_run_result_final")
BACKUP_DIR = Path("final_result_files/old")
BACKUP_PATH = BACKUP_DIR / "full_benchmark_results_summary_gold_corrected_before_rows_fill.xlsx"

EXCEL_CELL_LIMIT = 32767
CELL_MAX_CHARS = 32000
ILLEGAL_EXCEL_CHARS = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")
TRUNCATION_MARKER = "\n...(truncated; full rows are in the JSON result file)"
JSON_LOAD_SIZE_LIMIT_BYTES = 50_000_000


def clean_excel_string(value: str) -> str:
    return ILLEGAL_EXCEL_CHARS.sub("", value)


def load_result_json_header_only(path: Path) -> dict[str, Any]:
    prefix_lines = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if re.match(r'\s*"result"\s*:', line):
                break
            prefix_lines.append(line)
    header_text = "".join(prefix_lines) + '  "result": {"rows": []}\n}\n'
    return json.loads(header_text)


def rows_from_json(path: Path) -> list[Any]:
    if path.stat().st_size > JSON_LOAD_SIZE_LIMIT_BYTES:
        rec = load_result_json_header_only(path)
        observed = rec.get("observed") or {}
        return [
            {
                "__rows_omitted_due_to_large_json__": True,
                "row_count": observed.get("row_count"),
                "json_size_bytes": path.stat().st_size,
                "json_path": str(path),
            }
        ]

    with path.open("r", encoding="utf-8") as f:
        rec = json.load(f)
    rows = (rec.get("result") or {}).get("rows")
    if isinstance(rows, list):
        return rows
    return []


def compact_json(value: Any) -> tuple[str, bool]:
    if isinstance(value, list):
        pieces = ["["]
        current_len = 1
        for idx, item in enumerate(value):
            item_text = json.dumps(item, ensure_ascii=False, default=str, indent=2)
            item_text = "\n".join("  " + line for line in item_text.splitlines())
            prefix = "\n" if idx == 0 else ",\n"
            addition = prefix + item_text
            if current_len + len(addition) + 2 > CELL_MAX_CHARS:
                return clean_excel_string("".join(pieces) + TRUNCATION_MARKER), True
            pieces.append(addition)
            current_len += len(addition)
        pieces.append("\n]" if value else "]")
        text = clean_excel_string("".join(pieces))
        truncated = bool(value and isinstance(value[0], dict) and value[0].get("__rows_omitted_due_to_large_json__"))
        return text, truncated

    text = json.dumps(value, ensure_ascii=False, default=str, indent=2)
    text = clean_excel_string(text)
    if len(text) > CELL_MAX_CHARS:
        return text[: CELL_MAX_CHARS - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER, True
    if len(text) > EXCEL_CELL_LIMIT:
        return text[: EXCEL_CELL_LIMIT - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER, True
    return text, False


def header_map(ws) -> dict[str, int]:
    return {
        str(cell.value).strip(): cell.column
        for cell in ws[1]
        if cell.value is not None and str(cell.value).strip()
    }


def required_columns_present(headers: dict[str, int]) -> bool:
    required = {
        "request_id",
        "db_id",
        "model",
        "model_result_rows",
        "golden_result_rows",
    }
    return required.issubset(headers)


def update_sheet(ws) -> dict[str, int]:
    headers = header_map(ws)
    if not required_columns_present(headers):
        return {"rows_seen": 0, "rows_updated": 0, "missing_files": 0}

    rows_updated = 0
    missing_files = 0

    for row_idx in range(2, ws.max_row + 1):
        request_id = ws.cell(row_idx, headers["request_id"]).value
        db_id = ws.cell(row_idx, headers["db_id"]).value
        model = ws.cell(row_idx, headers["model"]).value
        if request_id is None or db_id is None or model is None:
            continue

        request_id = int(request_id)
        db_id = str(db_id).strip()
        model = str(model).strip()

        model_path = RESULT_DIR / model / db_id / f"{request_id}.json"
        golden_path = RESULT_DIR / "golden_new" / db_id / f"{request_id}.json"
        if not golden_path.exists():
            golden_path = RESULT_DIR / "golden" / db_id / f"{request_id}.json"

        if not model_path.exists() or not golden_path.exists():
            missing_files += 1
            continue

        model_text, model_truncated = compact_json(rows_from_json(model_path))
        golden_text, golden_truncated = compact_json(rows_from_json(golden_path))

        ws.cell(row_idx, headers["model_result_rows"]).value = model_text
        ws.cell(row_idx, headers["golden_result_rows"]).value = golden_text

        if "model_result_truncated" in headers:
            ws.cell(row_idx, headers["model_result_truncated"]).value = model_truncated
        if "golden_result_truncated" in headers:
            ws.cell(row_idx, headers["golden_result_truncated"]).value = golden_truncated
        if "model_json_path" in headers:
            ws.cell(row_idx, headers["model_json_path"]).value = str(model_path)
        if "golden_json_path" in headers:
            ws.cell(row_idx, headers["golden_json_path"]).value = str(golden_path)

        rows_updated += 1

    return {
        "rows_seen": max(ws.max_row - 1, 0),
        "rows_updated": rows_updated,
        "missing_files": missing_files,
    }


def main() -> None:
    if not WORKBOOK_PATH.exists():
        raise FileNotFoundError(WORKBOOK_PATH)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not BACKUP_PATH.exists():
        shutil.copy2(WORKBOOK_PATH, BACKUP_PATH)

    wb = load_workbook(WORKBOOK_PATH)
    totals = {}
    for ws in wb.worksheets:
        stats = update_sheet(ws)
        if stats["rows_seen"]:
            totals[ws.title] = stats

    wb.save(WORKBOOK_PATH)

    print(f"Updated {WORKBOOK_PATH.resolve()}")
    print(f"Backup: {BACKUP_PATH.resolve()}")
    for sheet, stats in totals.items():
        print(
            f"{sheet}: rows_seen={stats['rows_seen']} "
            f"rows_updated={stats['rows_updated']} missing_files={stats['missing_files']}"
        )


if __name__ == "__main__":
    main()
