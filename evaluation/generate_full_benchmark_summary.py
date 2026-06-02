import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
from openpyxl.styles import Font, PatternFill


RESULT_DIR = Path("sql_run_result_final")
MASTER_WORKBOOK = Path("master_results_final.xlsx")
OUT_XLSX = Path("full_benchmark_results_summary.xlsx")
MODELS = ["gpt4o", "claude", "gemini"]
MODEL_ORDER = {name: i for i, name in enumerate(MODELS)}
EXCEL_CELL_LIMIT = 32767
CELL_MAX_CHARS = 30000
ILLEGAL_EXCEL_CHARS = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")
TRUNCATION_MARKER = "\n...(truncated; full rows are in the JSON result file)"
JSON_LOAD_SIZE_LIMIT_BYTES = 50_000_000
HASH_ROW_LIMIT = 100_000


def clean_excel_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return ILLEGAL_EXCEL_CHARS.sub("", value)


def compact_json(value: Any, max_chars: int = CELL_MAX_CHARS) -> str:
    if isinstance(value, list):
        pieces = ["["]
        current_len = 1
        for idx, item in enumerate(value):
            item_text = json.dumps(item, ensure_ascii=False, default=str, indent=2)
            item_text = "\n".join("  " + line for line in item_text.splitlines())
            prefix = "\n" if idx == 0 else ",\n"
            addition = prefix + item_text
            if current_len + len(addition) + 2 > max_chars:
                return clean_excel_string("".join(pieces) + TRUNCATION_MARKER)
            pieces.append(addition)
            current_len += len(addition)
        pieces.append("\n]" if value else "]")
        return clean_excel_string("".join(pieces))

    text = json.dumps(value, ensure_ascii=False, default=str, indent=2)
    text = clean_excel_string(text)
    if len(text) > max_chars:
        return text[: max_chars - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return text


def compact_json_truncated(text: str) -> bool:
    return TRUNCATION_MARKER.strip() in str(text)


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except Exception:
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def canonical_row_values_only(row: Any) -> str:
    if isinstance(row, dict):
        values = [None if value is None else str(value) for value in row.values()]
        return json.dumps(
            sorted(values, key=lambda value: "" if value is None else value),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return json.dumps(row, sort_keys=True, default=str)


def rows_value_hash(rows: Iterable[Any]) -> str:
    h = hashlib.sha256()
    canonical = [canonical_row_values_only(row) for row in rows]
    for row_text in sorted(canonical):
        h.update(row_text.encode("utf-8"))
        h.update(b"\n")
    return "sha256:" + h.hexdigest()


def load_result_json_header_only(path: Path) -> Dict[str, Any]:
    prefix_lines = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if re.match(r'\s*"result"\s*:', line):
                break
            prefix_lines.append(line)
    header_text = "".join(prefix_lines) + '  "result": {"rows": []}\n}\n'
    return json.loads(header_text)


def load_result_json(path: Path) -> Dict[str, Any]:
    rows_omitted = path.stat().st_size > JSON_LOAD_SIZE_LIMIT_BYTES
    if rows_omitted:
        rec = load_result_json_header_only(path)
    else:
        with path.open("r", encoding="utf-8") as f:
            rec = json.load(f)
    observed = rec.get("observed") or {}
    rows = (rec.get("result") or {}).get("rows")
    if not isinstance(rows, list):
        rows = []
    if rows_omitted:
        rows = [
            {
                "__rows_omitted_due_to_large_json__": True,
                "row_count": observed.get("row_count"),
                "json_size_bytes": path.stat().st_size,
                "json_path": str(path),
            }
        ]
    metadata = rec.get("metadata") or {}
    return {
        "request_id": int(rec.get("request_id") or metadata.get("request_id")),
        "db_id": metadata.get("db_id") or path.parent.name,
        "question": rec.get("question") or metadata.get("question"),
        "sql": rec.get("sql"),
        "ok": bool(observed.get("ok")),
        "error": observed.get("error"),
        "row_count": observed.get("row_count"),
        "server_cpu_ms": observed.get("server_cpu_ms"),
        "server_elapsed_ms": observed.get("server_elapsed_ms"),
        "rows": rows,
        "rows_omitted": rows_omitted,
        "json_size_bytes": path.stat().st_size,
        "json_path": str(path),
    }


def load_results(source: str) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    base = RESULT_DIR / source
    for path in sorted(base.rglob("*.json")):
        rec = load_result_json(path)
        request_id = int(rec["request_id"])
        if request_id in out:
            raise ValueError(f"Duplicate request_id {request_id} for {source}: {path}")
        out[request_id] = rec
    return out


def compare_model_to_golden(model_rec: Dict[str, Any], golden_rec: Dict[str, Any]) -> Dict[str, Any]:
    golden_rows = golden_rec["rows"]
    model_rows = model_rec["rows"]
    golden_count = int(golden_rec["row_count"]) if golden_rec.get("row_count") is not None else len(golden_rows)
    model_count = int(model_rec["row_count"]) if model_rec.get("row_count") is not None else len(model_rows)

    row_count_match = 1.0 if golden_count == model_count else 0.0
    should_hash = (
        golden_rec["ok"]
        and model_rec["ok"]
        and golden_count == model_count
        and not golden_rec.get("rows_omitted")
        and not model_rec.get("rows_omitted")
        and golden_count <= HASH_ROW_LIMIT
        and model_count <= HASH_ROW_LIMIT
    )
    if should_hash:
        golden_hash = rows_value_hash(golden_rows)
        model_hash = rows_value_hash(model_rows)
        value_hash_match = golden_hash == model_hash
    else:
        golden_hash = "not_computed"
        model_hash = "not_computed"
        value_hash_match = False

    if not golden_rec["ok"]:
        accuracy = None
        match = "not_scorable"
        reason = "Golden query failed, so output accuracy cannot be scored against this baseline."
    elif not model_rec["ok"]:
        accuracy = 0.0
        match = "incorrect"
        reason = "Model query failed before producing a successful output."
    elif value_hash_match:
        accuracy = 1.0
        match = "correct"
        reason = "Model output values match golden output values. Column names, column order, and row order are ignored."
    elif golden_count != model_count:
        accuracy = 0.0
        match = "incorrect"
        reason = f"Row count differs: golden returned {golden_count} rows, model returned {model_count} rows."
    elif not should_hash:
        accuracy = 0.0
        match = "incorrect"
        reason = "Row count matches, but the result set is too large to hash in this summary; full rows remain in the JSON result file."
    else:
        accuracy = 0.0
        match = "incorrect"
        reason = "Row count matches, but one or more output values differ."

    golden_elapsed = safe_float(golden_rec.get("server_elapsed_ms"))
    model_elapsed = safe_float(model_rec.get("server_elapsed_ms"))
    if golden_elapsed is None or model_elapsed is None:
        runtime_delta_ms = None
        runtime_delta_pct = None
        runtime_missing = 1.0
    else:
        runtime_delta_ms = model_elapsed - golden_elapsed
        runtime_delta_pct = runtime_delta_ms / golden_elapsed if golden_elapsed != 0 else None
        runtime_missing = 0.0

    return {
        "actual_output_accuracy": accuracy,
        "actual_output_match": match,
        "actual_output_reason": reason,
        "actual_row_count_match": row_count_match,
        "value_hash_match": 1.0 if value_hash_match else 0.0,
        "golden_values_hash": golden_hash,
        "model_values_hash": model_hash,
        "golden_elapsed_ms": golden_elapsed,
        "actual_elapsed_ms": model_elapsed,
        "runtime_delta_ms": runtime_delta_ms,
        "runtime_delta_pct": runtime_delta_pct,
        "runtime_missing": runtime_missing,
    }


def build_summary() -> pd.DataFrame:
    master = pd.read_excel(MASTER_WORKBOOK)
    master["db_id"] = master["db_id"].astype(str).str.strip()
    question_by_id = {
        int(row["index"]): {
            "db_id": row["db_id"],
            "question": row["question"],
            "id": row.get("id"),
        }
        for _, row in master.iterrows()
    }

    golden = load_results("golden")
    models = {model: load_results(model) for model in MODELS}

    rows: List[Dict[str, Any]] = []
    for request_id in sorted(question_by_id):
        golden_rec = golden.get(request_id)
        if golden_rec is None:
            raise FileNotFoundError(f"Missing golden result for request_id={request_id}")

        for model in MODELS:
            model_rec = models[model].get(request_id)
            if model_rec is None:
                raise FileNotFoundError(f"Missing {model} result for request_id={request_id}")

            comparison = compare_model_to_golden(model_rec, golden_rec)
            model_result_rows = compact_json(model_rec["rows"])
            golden_result_rows = compact_json(golden_rec["rows"])
            q = question_by_id[request_id]
            rows.append(
                {
                    "request_id": request_id,
                    "source_id": q["id"],
                    "db_id": q["db_id"],
                    "question": q["question"],
                    "model": model,
                    **comparison,
                    "model_ok": model_rec["ok"],
                    "model_error": model_rec["error"],
                    "model_row_count": model_rec["row_count"],
                    "model_server_cpu_ms": model_rec["server_cpu_ms"],
                    "model_server_elapsed_ms": model_rec["server_elapsed_ms"],
                    "golden_ok": golden_rec["ok"],
                    "golden_error": golden_rec["error"],
                    "golden_row_count_observed": golden_rec["row_count"],
                    "golden_server_cpu_ms_observed": golden_rec["server_cpu_ms"],
                    "golden_server_elapsed_ms_observed": golden_rec["server_elapsed_ms"],
                    "model_result_rows": model_result_rows,
                    "golden_result_rows": golden_result_rows,
                    "model_result_truncated": compact_json_truncated(model_result_rows) or bool(model_rec.get("rows_omitted")),
                    "golden_result_truncated": compact_json_truncated(golden_result_rows) or bool(golden_rec.get("rows_omitted")),
                    "model_json_path": model_rec["json_path"],
                    "golden_json_path": golden_rec["json_path"],
                    "model_sql": clean_excel_string(model_rec["sql"]),
                    "golden_sql": clean_excel_string(golden_rec["sql"]),
                }
            )

    df = pd.DataFrame(rows)
    df["_model_order"] = df["model"].map(MODEL_ORDER)
    df = df.sort_values(["request_id", "_model_order"]).drop(columns=["_model_order"]).reset_index(drop=True)
    return df


def build_model_summaries(summary: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    scored = summary[summary["actual_output_accuracy"].notna()].copy()
    model_summary = scored.groupby("model", as_index=False).agg(
        scored_query_count=("actual_output_accuracy", "count"),
        actual_output_accuracy_rate=("actual_output_accuracy", "mean"),
        row_count_match_rate=("actual_row_count_match", "mean"),
        value_hash_match_rate=("value_hash_match", "mean"),
        avg_runtime_delta_ms=("runtime_delta_ms", "mean"),
        median_runtime_delta_ms=("runtime_delta_ms", "median"),
    )
    not_scorable = summary[summary["actual_output_accuracy"].isna()].groupby("model").size().rename("not_scorable_count")
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
        | (~summary["model_ok"])
        | (~summary["golden_ok"])
    ].copy()

    return {
        "accuracy_by_model": model_summary,
        "accuracy_by_model_db": model_db_summary,
        "execution_summary": execution_summary,
        "issues": issues,
    }


def write_excel(summary: pd.DataFrame, sheets: Dict[str, pd.DataFrame]) -> None:
    with pd.ExcelWriter(OUT_XLSX, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="full_benchmark_summary", index=False)
        for sheet_name, sheet_df in sheets.items():
            safe_name = sheet_name[:31]
            sheet_df.to_excel(writer, sheet_name=safe_name, index=False)

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
    summary = build_summary()
    sheets = build_model_summaries(summary)
    write_excel(summary, sheets)

    print(f"Created {OUT_XLSX.resolve()}")
    print(f"Full summary rows: {len(summary)}")
    print(sheets["accuracy_by_model"].to_string(index=False))


if __name__ == "__main__":
    main()
