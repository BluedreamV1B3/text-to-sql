import argparse
import hashlib
import json
import math
import os
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
from openai import AuthenticationError, OpenAI
from openpyxl.styles import Font, PatternFill


DEFAULT_MASTER = Path("master_results_final.xlsx")
DEFAULT_RESULTS_DIR = Path("sql_run_result_final")
DEFAULT_SUMMARY = Path("full_benchmark_results_summary.xlsx")
DEFAULT_OUTPUT = Path("golden_query_inspection.xlsx")
DEFAULT_CHECKPOINT = Path("golden_query_inspection_checkpoint.jsonl")
MODELS = ["gpt4o", "claude", "gemini"]

FLAG_LLM_CONSENSUS = "llm_consensus_disagrees_with_gold"
FLAG_GOLD_FAILED = "golden_query_failed"
FLAG_GOLD_EMPTY = "golden_returned_empty"


REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "issue_tags_all": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "no_issue",
                    "gold_incorrect",
                    "question_ambiguous",
                    "multiple_valid_answers",
                    "others",
                ],
            },
        },
        "issue_llm": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["gold_incorrect", "question_ambiguous", "others"],
            },
        },
        "explanation_llm": {"type": "string"},
        "new_gold_sql": {"type": "string"},
    },
    "required": ["issue_tags_all", "issue_llm", "explanation_llm", "new_gold_sql"],
}


SYSTEM_PROMPT = """You are a meticulous text-to-SQL benchmark auditor.

Your task is to review one question and its current ground-truth SQL query against the provided database schema.

Important rules:
- The target SQL dialect is Microsoft SQL Server / T-SQL.
- Mark ground-truth SQL that uses non-SQL-Server syntax as incorrect. Examples include LIMIT, SQLite-only behavior, unsupported ORDER BY usage, or invalid identifiers for SQL Server.
- Check whether the SQL uses tables/columns that exist in the schema.
- Check whether the SQL actually answers the natural-language question.
- Do not rely on model-generated answers. Only use the question, schema, and current ground-truth SQL.
- If there is no issue, set issue_tags_all to ["no_issue"], issue_llm to [], and new_gold_sql to "".
- For issue_llm, use only these tags: gold_incorrect, question_ambiguous, others.
- If the question is ambiguous, underspecified, impossible to answer from the schema, or can reasonably have multiple answers, include question_ambiguous.
- If the ground-truth SQL is wrong, does not match the schema, does not answer the question, or is not valid SQL Server syntax, include gold_incorrect.
- Use others only for issues that are not gold_incorrect or question_ambiguous.
- If gold_incorrect and you can confidently write a corrected SQL Server query, put it in new_gold_sql. Otherwise leave new_gold_sql empty.
- Keep explanations concise but specific.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flag and LLM-review ground-truth SQL queries for the text-to-SQL benchmark."
    )
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--model", default="gpt-5")
    parser.add_argument("--limit", type=int, default=None, help="Optional number of rows to review.")
    parser.add_argument("--start-after", type=int, default=None, help="Skip request_ids <= this value.")
    parser.add_argument("--force", action="store_true", help="Ignore checkpointed reviews and call the LLM again.")
    parser.add_argument("--no-llm", action="store_true", help="Only write heuristic flags; leave LLM columns empty.")
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--retry-base-seconds", type=float, default=2.0)
    parser.add_argument("--reasoning-effort", default="medium", choices=["minimal", "low", "medium", "high"])
    return parser.parse_args()


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


def normalize_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    try:
        with localcontext() as ctx:
            ctx.prec = max(50, len(value.as_tuple().digits))
            ctx.Emax = 999999999
            ctx.Emin = -999999999
            normalized = value.normalize(context=ctx)
        fixed = format(normalized, "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    except Exception:
        return str(value)


def normalize_date_string(text: str) -> Optional[str]:
    candidates = [
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ]
    for fmt in candidates:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
                return dt.date().isoformat()
            return dt.isoformat(sep=" ", timespec="microseconds").rstrip("0").rstrip(".")
        except ValueError:
            continue
    return None


def normalize_scalar(value: Any) -> str:
    if value is None:
        return "null:"
    if isinstance(value, bool):
        return f"bool:{str(value).lower()}"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"num:{value}"
    if isinstance(value, float):
        return f"num:{normalize_decimal(Decimal(str(value)))}"

    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)

    date_text = normalize_date_string(text)
    if date_text is not None:
        return f"date:{date_text}"

    if re.fullmatch(r"[-+]?(?:\d+\.\d+|\d+|\.\d+)(?:[eE][-+]?\d+)?", text):
        try:
            return f"num:{normalize_decimal(Decimal(text))}"
        except InvalidOperation:
            pass

    return f"str:{text}"


def canonical_row_values_only(row: Any) -> str:
    if isinstance(row, dict):
        normalized_values = [normalize_scalar(value) for value in row.values()]
        return json.dumps(sorted(normalized_values), ensure_ascii=False, separators=(",", ":"))
    return normalize_scalar(row)


def output_hash(rows: Iterable[Any]) -> str:
    h = hashlib.sha256()
    canonical_rows = [canonical_row_values_only(row) for row in rows]
    for row_text in sorted(canonical_rows):
        h.update(row_text.encode("utf-8"))
        h.update(b"\n")
    return "sha256:" + h.hexdigest()


def load_result(path: Path, *, include_hash: bool = True) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        rec = json.load(f)
    metadata = rec.get("metadata") or {}
    observed = rec.get("observed") or {}
    rows = (rec.get("result") or {}).get("rows")
    if not isinstance(rows, list):
        rows = []
    request_id = int(rec.get("request_id") or metadata.get("request_id"))
    return {
        "request_id": request_id,
        "db_id": metadata.get("db_id") or path.parent.name,
        "question": rec.get("question") or metadata.get("question"),
        "sql": rec.get("sql") or "",
        "ok": bool(observed.get("ok")),
        "error": observed.get("error"),
        "row_count": observed.get("row_count"),
        "server_elapsed_ms": observed.get("server_elapsed_ms"),
        "rows": rows,
        "hash": output_hash(rows) if include_hash else None,
        "json_path": str(path),
    }


def load_source_results(results_dir: Path, source: str, *, include_hash: bool = True) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for path in sorted((results_dir / source).rglob("*.json")):
        rec = load_result(path, include_hash=include_hash)
        request_id = rec["request_id"]
        if request_id in out:
            raise ValueError(f"Duplicate {source} result for request_id={request_id}: {path}")
        out[request_id] = rec
    return out


def schema_file_for_db(db_id: str) -> Path:
    if db_id == "BiomedSQL":
        return Path("BiomedSQL") / "schema_biomed.py"
    if db_id == "Insurance":
        return Path("Insurance") / "schema_insurance.py"
    return Path("KaggleDBQA") / "schemas" / f"schema_{db_id}.py"


def determine_flag(golden: Dict[str, Any], model_results: Dict[str, Dict[str, Any]]) -> str:
    if not golden["ok"]:
        return FLAG_GOLD_FAILED
    if len(golden["rows"]) == 0:
        return FLAG_GOLD_EMPTY

    hash_to_models: Dict[str, List[str]] = {}
    for model_name, result in model_results.items():
        if result and result["ok"]:
            hash_to_models.setdefault(result["hash"], []).append(model_name)

    consensus_hashes = [
        hash_value
        for hash_value, agreeing_models in hash_to_models.items()
        if len(agreeing_models) >= 2
    ]
    for hash_value in consensus_hashes:
        if hash_value != golden["hash"]:
            return FLAG_LLM_CONSENSUS

    return ""


def determine_flags_from_summary(summary_path: Path) -> Dict[int, str]:
    summary = pd.read_excel(
        summary_path,
        sheet_name="full_benchmark_summary",
        usecols=[
            "request_id",
            "model",
            "golden_ok",
            "model_ok",
            "golden_row_count_observed",
            "golden_values_hash",
            "model_values_hash",
        ],
    )
    flags: Dict[int, str] = {}
    for request_id, group in summary.groupby("request_id"):
        golden_ok = bool(group["golden_ok"].iloc[0])
        golden_row_count = safe_float(group["golden_row_count_observed"].iloc[0])
        golden_hash = str(group["golden_values_hash"].iloc[0])

        if not golden_ok:
            flags[int(request_id)] = FLAG_GOLD_FAILED
            continue
        if golden_row_count == 0:
            flags[int(request_id)] = FLAG_GOLD_EMPTY
            continue

        hash_to_models: Dict[str, List[str]] = {}
        for _, row in group.iterrows():
            if bool(row["model_ok"]):
                hash_to_models.setdefault(str(row["model_values_hash"]), []).append(str(row["model"]))

        flag = ""
        for model_hash, agreeing_models in hash_to_models.items():
            if len(agreeing_models) >= 2 and model_hash != golden_hash:
                flag = FLAG_LLM_CONSENSUS
                break
        flags[int(request_id)] = flag
    return flags


def load_checkpoint(path: Path) -> Dict[int, Dict[str, Any]]:
    if not path.exists():
        return {}
    out: Dict[int, Dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out[int(rec["request_id"])] = rec
    return out


def append_checkpoint(path: Path, record: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def extract_response_json(response: Any) -> Dict[str, Any]:
    text = getattr(response, "output_text", None)
    if not text:
        # Defensive fallback for older SDK response shapes.
        try:
            text = response.output[0].content[0].text
        except Exception as exc:
            raise RuntimeError(f"Could not extract text from OpenAI response: {response}") from exc
    return json.loads(text)


def call_reviewer(
    client: OpenAI,
    *,
    model: str,
    reasoning_effort: str,
    max_retries: int,
    retry_base_seconds: float,
    request_id: int,
    db_id: str,
    question: str,
    schema_file: Path,
    schema_text: str,
    original_gold_sql: str,
) -> Dict[str, Any]:
    user_payload = {
        "request_id": request_id,
        "db_id": db_id,
        "schema_file": str(schema_file),
        "question": question,
        "original_gold_sql": original_gold_sql,
        "database_schema_python_file_contents": schema_text,
    }
    user_prompt = (
        "Review this question/ground-truth SQL pair. Return only JSON matching the schema.\n\n"
        + json.dumps(user_payload, ensure_ascii=False, indent=2)
    )

    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                reasoning={"effort": reasoning_effort},
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "golden_query_review",
                        "schema": REVIEW_SCHEMA,
                        "strict": True,
                    }
                },
            )
            review = extract_response_json(response)
            return normalize_review(review)
        except Exception as exc:
            if isinstance(exc, AuthenticationError):
                raise RuntimeError(f"OpenAI authentication failed: {exc}") from exc
            last_error = exc
            if attempt >= max_retries:
                break
            time.sleep(retry_base_seconds * (2 ** (attempt - 1)))

    return {
        "issue_tags_all": ["others"],
        "issue_llm": ["others"],
        "explanation_llm": f"LLM review failed after {max_retries} attempts: {type(last_error).__name__}: {last_error}",
        "new_gold_sql": "",
    }


def normalize_review(review: Dict[str, Any]) -> Dict[str, Any]:
    issue_tags_all = review.get("issue_tags_all") or []
    issue_llm = review.get("issue_llm") or []
    explanation = str(review.get("explanation_llm") or "").strip()
    new_gold_sql = str(review.get("new_gold_sql") or "").strip()

    allowed_all = {"no_issue", "gold_incorrect", "question_ambiguous", "multiple_valid_answers", "others"}
    allowed_issue = {"gold_incorrect", "question_ambiguous", "others"}
    issue_tags_all = [tag for tag in issue_tags_all if tag in allowed_all]
    issue_llm = [tag for tag in issue_llm if tag in allowed_issue]

    if "multiple_valid_answers" in issue_tags_all and "question_ambiguous" not in issue_llm:
        issue_llm.append("question_ambiguous")

    if not issue_tags_all and not issue_llm:
        issue_tags_all = ["no_issue"]

    if issue_tags_all == ["no_issue"]:
        issue_llm = []
        new_gold_sql = ""

    return {
        "issue_tags_all": issue_tags_all,
        "issue_llm": issue_llm,
        "explanation_llm": explanation,
        "new_gold_sql": new_gold_sql,
    }


def build_base_records(master_path: Path, results_dir: Path, summary_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    master = pd.read_excel(master_path)
    master["db_id"] = master["db_id"].astype(str).str.strip()
    master_rows = {
        int(row["index"]): {
            "request_id": int(row["index"]),
            "db_id": row["db_id"],
            "question": row["question"],
        }
        for _, row in master.iterrows()
    }

    golden_results = load_source_results(results_dir, "golden", include_hash=False)
    summary_flags = determine_flags_from_summary(summary_path) if summary_path and summary_path.exists() else None
    model_results_by_name = None if summary_flags is not None else {model: load_source_results(results_dir, model) for model in MODELS}

    records: List[Dict[str, Any]] = []
    for request_id in sorted(master_rows):
        row = master_rows[request_id]
        golden = golden_results.get(request_id)
        if golden is None:
            raise FileNotFoundError(f"Missing golden result for request_id={request_id}")
        if summary_flags is not None:
            flag = summary_flags.get(request_id, "")
        else:
            model_results = {model: model_results_by_name[model].get(request_id) for model in MODELS}
            missing_models = [model for model, result in model_results.items() if result is None]
            if missing_models:
                raise FileNotFoundError(f"Missing model result(s) for request_id={request_id}: {missing_models}")
            flag = determine_flag(golden, model_results)

        schema_file = schema_file_for_db(row["db_id"])
        if not schema_file.exists():
            raise FileNotFoundError(f"Missing schema file for db_id={row['db_id']}: {schema_file}")

        records.append(
            {
                "request_id": request_id,
                "db_id": row["db_id"],
                "question": row["question"],
                "flag": flag,
                "schema_file": str(schema_file),
                "original_gold_sql": golden["sql"],
                "_golden_ok": golden["ok"],
                "_golden_row_count": len(golden["rows"]),
                "_golden_error": golden["error"],
            }
        )
    return records


def write_excel(records: List[Dict[str, Any]], output_path: Path) -> None:
    columns = [
        "request_id",
        "db_id",
        "question",
        "flag",
        "schema_file",
        "original_gold_sql",
        "issue_llm",
        "explanation_llm",
        "new_gold_sql",
    ]
    df = pd.DataFrame([{col: rec.get(col, "") for col in columns} for rec in records])
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="golden_query_inspection", index=False)
        ws = writer.book["golden_query_inspection"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill(fill_type="solid", fgColor="D9EAF7")
        widths = {
            "A": 12,
            "B": 24,
            "C": 70,
            "D": 34,
            "E": 48,
            "F": 80,
            "G": 28,
            "H": 90,
            "I": 80,
        }
        for letter, width in widths.items():
            ws.column_dimensions[letter].width = width
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = cell.alignment.copy(wrap_text=True, vertical="top")


def main() -> None:
    args = parse_args()
    if (not args.no_llm) and (not os.environ.get("OPENAI_API_KEY")):
        raise RuntimeError("OPENAI_API_KEY is not set. Set it in the environment before running this script.")

    records = build_base_records(args.master, args.results_dir, args.summary)
    if args.start_after is not None:
        records = [rec for rec in records if rec["request_id"] > args.start_after]
    if args.limit is not None:
        records = records[: args.limit]

    if args.no_llm:
        reviewed_records = []
        for rec in records:
            out_rec = dict(rec)
            out_rec["issue_llm"] = ""
            out_rec["explanation_llm"] = ""
            out_rec["new_gold_sql"] = ""
            reviewed_records.append(out_rec)
        write_excel(reviewed_records, args.output)
        flagged_count = sum(1 for rec in reviewed_records if rec.get("flag"))
        print(f"Created {args.output.resolve()}")
        print(f"Rows written: {len(reviewed_records)}")
        print(f"Heuristic flagged rows: {flagged_count}")
        print("LLM review skipped because --no-llm was provided.")
        return

    schema_cache: Dict[str, str] = {}
    checkpoint = {} if args.force else load_checkpoint(args.checkpoint)
    client = OpenAI()

    reviewed_records: List[Dict[str, Any]] = []
    total = len(records)
    for idx, rec in enumerate(records, start=1):
        request_id = int(rec["request_id"])
        if request_id in checkpoint:
            review = checkpoint[request_id]
        else:
            schema_path = Path(rec["schema_file"])
            if rec["schema_file"] not in schema_cache:
                schema_cache[rec["schema_file"]] = schema_path.read_text(encoding="utf-8")
            print(f"[{idx}/{total}] reviewing request_id={request_id} db_id={rec['db_id']} flag={rec['flag'] or 'none'}")
            review = call_reviewer(
                client,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                max_retries=args.max_retries,
                retry_base_seconds=args.retry_base_seconds,
                request_id=request_id,
                db_id=rec["db_id"],
                question=rec["question"],
                schema_file=schema_path,
                schema_text=schema_cache[rec["schema_file"]],
                original_gold_sql=rec["original_gold_sql"],
            )
            review = {"request_id": request_id, **review}
            append_checkpoint(args.checkpoint, review)

        out_rec = dict(rec)
        issue_llm = review.get("issue_llm") or []
        out_rec["issue_llm"] = ";".join(issue_llm)
        out_rec["explanation_llm"] = review.get("explanation_llm") or ""
        out_rec["new_gold_sql"] = review.get("new_gold_sql") or ""
        reviewed_records.append(out_rec)

        # Keep an up-to-date Excel file even during a long run.
        if idx % 25 == 0:
            write_excel(reviewed_records, args.output)

    write_excel(reviewed_records, args.output)
    flagged_count = sum(1 for rec in reviewed_records if rec.get("flag"))
    issue_count = sum(1 for rec in reviewed_records if rec.get("issue_llm"))
    print(f"Created {args.output.resolve()}")
    print(f"Rows written: {len(reviewed_records)}")
    print(f"Heuristic flagged rows: {flagged_count}")
    print(f"Rows with LLM issue tags: {issue_count}")


if __name__ == "__main__":
    main()
