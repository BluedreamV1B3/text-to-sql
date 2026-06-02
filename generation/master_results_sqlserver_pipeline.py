from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from run_full_experiment import (
    DATABASES,
    build_system_prompt,
    initialize_llms,
    load_schema,
    safe_model_call,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MASTER_PATH = PROJECT_ROOT / "benchmark" / "workbooks" / "master_results_batch_1.xlsx"
_SCHEMA_CACHE: dict[str, dict] | None = None

DROP_COLUMNS = [
    "explanation_gpt4o",
    "explanation_claude",
    "explanation_germini",
    "explanation_gemini",
    "sql_len_gpt4o",
    "tokens_gpt4o",
    "sql_len_claude",
    "tokens_claude",
    "sql_len_germini",
    "tokens_gemini",
    "query",
]

MODEL_SQL_COLUMNS = {
    "gpt4o": "sql_gpt4o",
    "claude": "sql_claude",
    "gemini": "sql_germini",
}

MODEL_SUBMIT_COLUMNS = {
    "gpt4o": "sql_submit_gpt4o",
    "claude": "sql_submit_claude",
    "gemini": "sql_submit_gemini",
}

FINAL_COLUMN_ORDER = [
    "index",
    "id",
    "db_id",
    "question",
    "sql_gpt4o",
    "sql_claude",
    "sql_germini",
    "query_ground_truth",
    "golden_set",
    "sql_submit_gpt4o",
    "sql_submit_claude",
    "sql_submit_gemini",
]

SQL_SERVER_MARKERS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bLIMIT\b",
        r"`",
        r"\bILIKE\b",
        r"::",
        r"\|\|",
        r"\bTRUE\b",
        r"\bFALSE\b",
        r"\bDATE_TRUNC\s*\(",
        r"\bSTRFTIME\s*\(",
    ]
]


def backup_workbook(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.stem}.backup_{timestamp}{path.suffix}")
    shutil.copy2(path, backup)
    return backup


def is_blank(value) -> bool:
    if pd.isna(value):
        return True
    return str(value).strip() == ""


def strip_sql_comments(sql: str) -> str:
    result: list[str] = []
    in_single_quote = False
    i = 0

    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""

        if ch == "'":
            result.append(ch)
            if in_single_quote and nxt == "'":
                result.append(nxt)
                i += 2
                continue
            in_single_quote = not in_single_quote
            i += 1
            continue

        if not in_single_quote and ch == "-" and nxt == "-":
            i += 2
            while i < len(sql) and sql[i] not in "\r\n":
                i += 1
            continue

        if not in_single_quote and ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(sql) and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2 if i + 1 < len(sql) else 0
            continue

        result.append(ch)
        i += 1

    return "".join(result)


def normalize_sql(sql) -> str:
    if is_blank(sql):
        return ""
    raw_sql = str(sql)
    uncommented = strip_sql_comments(raw_sql).strip()
    if not uncommented:
        query_start = re.search(r"(?is)\b(SELECT|WITH)\b", raw_sql)
        if query_start:
            uncommented = raw_sql[query_start.start():]

    cleaned = " ".join(uncommented.split())
    return cleaned.rstrip(";") + ";"


def has_sql_server_conversion_marker(sql) -> bool:
    if is_blank(sql):
        return False
    return any(pattern.search(str(sql)) for pattern in SQL_SERVER_MARKERS)


def quote_depths(sql: str) -> tuple[list[int], list[bool]]:
    depths = [0] * len(sql)
    quoted = [False] * len(sql)
    depth = 0
    in_single_quote = False
    i = 0

    while i < len(sql):
        depths[i] = depth
        quoted[i] = in_single_quote
        ch = sql[i]

        if ch == "'":
            if in_single_quote and i + 1 < len(sql) and sql[i + 1] == "'":
                i += 1
                depths[i] = depth
                quoted[i] = True
            else:
                in_single_quote = not in_single_quote
        elif not in_single_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)

        i += 1

    return depths, quoted


def regex_matches_outside_quotes(pattern: re.Pattern, sql: str) -> list[re.Match]:
    _, quoted = quote_depths(sql)
    return [m for m in pattern.finditer(sql) if not quoted[m.start()]]


def insert_top(sql: str, select_match: re.Match, limit_value: str) -> str:
    insert_at = select_match.end()
    after_select = sql[insert_at:]
    modifier = re.match(r"\s+(DISTINCT|ALL)\b", after_select, flags=re.IGNORECASE)
    if modifier:
        insert_at += modifier.end()

    if re.match(r"\s+TOP\s*(\(|\d)", sql[insert_at:], flags=re.IGNORECASE):
        return sql

    return f"{sql[:insert_at]} TOP ({limit_value}){sql[insert_at:]}"


def convert_limits_to_top(sql: str) -> str:
    select_re = re.compile(r"\bSELECT\b", flags=re.IGNORECASE)
    limit_re = re.compile(r"\bLIMIT\s+(\d+)\b\s*", flags=re.IGNORECASE)
    converted = sql

    while True:
        limits = regex_matches_outside_quotes(limit_re, converted)
        if not limits:
            return converted

        limit_match = limits[-1]
        limit_value = limit_match.group(1)
        depths, quoted = quote_depths(converted)
        limit_depth = depths[limit_match.start()]

        select_match = None
        for candidate in reversed(list(select_re.finditer(converted, 0, limit_match.start()))):
            if quoted[candidate.start()]:
                continue
            if depths[candidate.start()] == limit_depth:
                select_match = candidate
                break

        if select_match is None:
            return converted

        converted = f"{converted[:limit_match.start()]}{converted[limit_match.end():]}"
        converted = insert_top(converted, select_match, limit_value)


def deterministic_tsql_conversion(sql) -> str:
    if is_blank(sql):
        return ""

    converted = str(sql).strip()

    def replace_backtick(match: re.Match) -> str:
        identifier = match.group(1)
        return identifier.split(".")[-1]

    converted = re.sub(r"`([^`]+)`", replace_backtick, converted)
    converted = re.sub(
        r'"([^"\']*)"',
        lambda match: "'" + match.group(1).replace("'", "''") + "'",
        converted,
    )
    converted = re.sub(r"=\s*TRUE\b", "= 1", converted, flags=re.IGNORECASE)
    converted = re.sub(r"=\s*FALSE\b", "= 0", converted, flags=re.IGNORECASE)
    converted = re.sub(r"\bTOP\s+(\d+)\b", r"TOP (\1)", converted, flags=re.IGNORECASE)
    converted = convert_limits_to_top(converted)
    return normalize_sql(converted)


def preserve_generated_sql(sql) -> str:
    if is_blank(sql):
        return ""
    return str(sql)


def make_submit(
    sql,
    model: str,
    request_id: int,
    question: str,
    run: int,
    normalize_query: bool = True,
) -> str:
    sql_value = normalize_sql(sql) if normalize_query else preserve_generated_sql(sql)
    sql_literal = json.dumps(sql_value, ensure_ascii=False)
    model_literal = json.dumps(model, ensure_ascii=False)
    question_literal = json.dumps(str(question), ensure_ascii=False)
    return (
        f"runner.submit({sql_literal}, "
        f"metadata={{\"run\":{run}, \"model\":{model_literal}, "
        f"\"request_id\":{int(request_id)}, \"question\":{question_literal}}})"
    )


def request_id_for_row(row: pd.Series) -> int:
    if "index" in row and not pd.isna(row["index"]):
        return int(row["index"])
    return int(row.name) + 1


def clean_master_dataframe(
    df: pd.DataFrame,
    reset_model_sql: bool,
    ground_truth_converter: str,
    run: int,
) -> tuple[pd.DataFrame, dict[str, int]]:
    summary = {
        "dropped_columns": 0,
        "reset_model_sql_cells": 0,
        "converted_ground_truth": 0,
        "ground_truth_markers_remaining": 0,
    }

    redundant = [col for col in DROP_COLUMNS if col in df.columns]
    summary["dropped_columns"] = len(redundant)
    df = df.drop(columns=redundant)

    if "query (cleaned)" in df.columns:
        df = df.rename(columns={"query (cleaned)": "query_ground_truth"})

    if "sql_gemini" in df.columns and "sql_germini" not in df.columns:
        df = df.rename(columns={"sql_gemini": "sql_germini"})

    for col in FINAL_COLUMN_ORDER:
        if col not in df.columns:
            df[col] = ""

    if reset_model_sql:
        for col in MODEL_SQL_COLUMNS.values():
            summary["reset_model_sql_cells"] += int((~df[col].isna() & (df[col].astype(str).str.strip() != "")).sum())
            df[col] = ""
        for col in MODEL_SUBMIT_COLUMNS.values():
            df[col] = ""

    if ground_truth_converter != "none":
        for row_idx in df.index:
            original = df.at[row_idx, "query_ground_truth"]
            if is_blank(original):
                continue
            if ground_truth_converter == "heuristic":
                converted = deterministic_tsql_conversion(original)
            else:
                if has_sql_server_conversion_marker(original):
                    converted = convert_ground_truth_with_openai(
                        sql=str(original),
                        question=str(df.at[row_idx, "question"]),
                        db_id=str(df.at[row_idx, "db_id"]),
                    )
                else:
                    converted = normalize_sql(original)
            if converted != normalize_sql(original):
                summary["converted_ground_truth"] += 1
            df.at[row_idx, "query_ground_truth"] = converted

    for row_idx in df.index:
        row = df.loc[row_idx]
        question = str(row["question"])
        request_id = request_id_for_row(row)
        ground_truth_sql = df.at[row_idx, "query_ground_truth"]
        if is_blank(ground_truth_sql):
            df.at[row_idx, "golden_set"] = ""
        else:
            df.at[row_idx, "golden_set"] = make_submit(
                ground_truth_sql,
                model="golden",
                request_id=request_id,
                question=question,
                run=run,
            )

        for model, sql_col in MODEL_SQL_COLUMNS.items():
            submit_col = MODEL_SUBMIT_COLUMNS[model]
            sql = df.at[row_idx, sql_col]
            df.at[row_idx, submit_col] = "" if is_blank(sql) else make_submit(
                sql,
                model=model,
                request_id=request_id,
                question=question,
                run=run,
                normalize_query=False,
            )

    summary["ground_truth_markers_remaining"] = int(
        df["query_ground_truth"].fillna("").astype(str).apply(has_sql_server_conversion_marker).sum()
    )

    remaining = [col for col in FINAL_COLUMN_ORDER if col in df.columns]
    extras = [col for col in df.columns if col not in remaining]
    return df[remaining + extras], summary


def schemas_by_db() -> dict[str, dict]:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        _SCHEMA_CACHE = {
            db_info["db_id"]: load_schema(db_info["schema_path"])
            for db_info in DATABASES
        }
    return _SCHEMA_CACHE


def convert_ground_truth_with_openai(sql: str, question: str, db_id: str) -> str:
    from openai import OpenAI

    model = os.getenv("GROUND_TRUTH_CONVERTER_MODEL", "gpt-4o-mini")
    schema = schemas_by_db()[db_id]
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    system = (
        "You convert SQL queries to Microsoft SQL Server T-SQL. "
        "Preserve the original query's semantics and make the minimum required dialect changes. "
        "Use only table and column names present in the schema. "
        "Return JSON with keys sql and notes. The sql value must contain only one executable T-SQL query."
    )
    user = {
        "db_id": db_id,
        "question": question,
        "schema": schema,
        "sql_to_convert": sql,
        "rules": [
            "Do not use LIMIT; use TOP (n).",
            "Do not use backticks or BigQuery project/dataset prefixes.",
            "Do not use PostgreSQL casts, ILIKE, ||, DATE_TRUNC, or STRFTIME.",
            "Use 1/0 for bit or boolean comparisons.",
            "Keep the table and column choices unchanged unless the schema requires a name correction.",
        ],
    }

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    parsed = json.loads(response.choices[0].message.content)
    return normalize_sql(parsed["sql"])


def selected_rows(
    df: pd.DataFrame,
    row_start: int | None,
    row_end: int | None,
    row_limit: int | None,
    sample_per_db: int | None,
    sample_seed: int,
) -> list[int]:
    mask = pd.Series(True, index=df.index)
    if row_start is not None:
        mask &= df["index"].astype(int) >= row_start
    if row_end is not None:
        mask &= df["index"].astype(int) <= row_end
    rows = list(df.index[mask])

    if sample_per_db is not None:
        sampled_rows = []
        filtered = df.loc[rows]
        for _, group in filtered.groupby("db_id", sort=False):
            n = min(sample_per_db, len(group))
            sampled_rows.extend(group.sample(n=n, random_state=sample_seed).index.tolist())
        rows = sorted(sampled_rows, key=lambda idx: int(df.at[idx, "index"]))

    if row_limit is not None:
        rows = rows[:row_limit]
    return rows


def parse_models(models: str) -> list[str]:
    requested = [model.strip() for model in models.split(",") if model.strip()]
    unsupported = sorted(set(requested) - set(MODEL_SQL_COLUMNS))
    if unsupported:
        raise ValueError(f"Unsupported model(s): {unsupported}")
    return requested


def generate_model_sql(
    df: pd.DataFrame,
    output_path: Path,
    models: Iterable[str],
    run: int,
    skip_existing: bool,
    row_start: int | None,
    row_end: int | None,
    row_limit: int | None,
    sample_per_db: int | None,
    sample_seed: int,
    checkpoint_frequency: int,
) -> pd.DataFrame:
    llm_pool = initialize_llms()
    schema_cache = schemas_by_db()
    for col in list(MODEL_SQL_COLUMNS.values()) + list(MODEL_SUBMIT_COLUMNS.values()):
        if col in df.columns:
            df[col] = df[col].astype("object").fillna("")

    rows = selected_rows(df, row_start, row_end, row_limit, sample_per_db, sample_seed)
    completed_calls = 0
    print(f"Selected {len(rows)} row(s) for generation.")
    if sample_per_db is not None:
        selected = df.loc[rows, ["index", "db_id", "id"]]
        for db_id, group in selected.groupby("db_id", sort=False):
            request_ids = ", ".join(str(int(v)) for v in group["index"])
            print(f"  sample {db_id}: request_id(s) {request_ids}")

    for db_id, db_rows in df.loc[rows].groupby("db_id", sort=False).groups.items():
        schema = schema_cache[str(db_id)]
        for model in models:
            model_info = llm_pool[model]
            model_info["llm"].configuration["prompt"]["system"] = build_system_prompt(
                model_info["base_prompt"],
                schema,
            )

        for row_idx in db_rows:
            question = str(df.at[row_idx, "question"])
            request_id = request_id_for_row(df.loc[row_idx])
            print(f"[{request_id}] {db_id}: {question[:90]}")

            for model in models:
                sql_col = MODEL_SQL_COLUMNS[model]
                submit_col = MODEL_SUBMIT_COLUMNS[model]

                if skip_existing and not is_blank(df.at[row_idx, sql_col]):
                    print(f"  - {model}: already filled, skipped")
                    continue

                response = safe_model_call(llm_pool[model]["llm"], question, model)
                sql = preserve_generated_sql(response.get("sql", ""))
                df.at[row_idx, sql_col] = sql
                df.at[row_idx, submit_col] = "" if is_blank(sql) else make_submit(
                    sql,
                    model=model,
                    request_id=request_id,
                    question=question,
                    run=run,
                    normalize_query=False,
                )
                completed_calls += 1
                print(f"  - {model}: {'filled' if sql else 'empty'}")

                if checkpoint_frequency > 0 and completed_calls % checkpoint_frequency == 0:
                    df.to_excel(output_path, index=False)
                    print(f"  checkpoint saved after {completed_calls} model call(s)")

    df.to_excel(output_path, index=False)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean and rerun the master Text-to-SQL benchmark workbook for SQL Server."
    )
    parser.add_argument("--input", type=Path, default=MASTER_PATH)
    parser.add_argument("--output", type=Path, default=MASTER_PATH)
    parser.add_argument("--prepare", action="store_true", help="Clean columns, reset SQL, convert ground truth, and refresh submit strings.")
    parser.add_argument("--generate", action="store_true", help="Call the LLMs and fill model SQL columns.")
    parser.add_argument("--run", type=int, default=1)
    parser.add_argument("--ground-truth-converter", choices=["none", "heuristic", "openai"], default="heuristic")
    parser.add_argument("--reset-model-sql", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--backup", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--models", default="gpt4o,claude,gemini")
    parser.add_argument("--skip-existing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--row-start", type=int)
    parser.add_argument("--row-end", type=int)
    parser.add_argument("--row-limit", type=int)
    parser.add_argument("--sample-per-db", type=int)
    parser.add_argument("--sample-seed", type=int, default=2026)
    parser.add_argument("--checkpoint-frequency", type=int, default=10)
    args = parser.parse_args()

    if not args.prepare and not args.generate:
        args.prepare = True

    if args.backup and args.input.exists() and args.input.resolve() == args.output.resolve():
        backup = backup_workbook(args.input)
        print(f"Backup created: {backup}")

    df = pd.read_excel(args.input)

    if args.prepare:
        df, summary = clean_master_dataframe(
            df,
            reset_model_sql=args.reset_model_sql,
            ground_truth_converter=args.ground_truth_converter,
            run=args.run,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        df.to_excel(args.output, index=False)
        print("Prepare summary:")
        for key, value in summary.items():
            print(f"  {key}: {value}")

    if args.generate:
        start_time = time.time()
        df = generate_model_sql(
            df,
            output_path=args.output,
            models=parse_models(args.models),
            run=args.run,
            skip_existing=args.skip_existing,
            row_start=args.row_start,
            row_end=args.row_end,
            row_limit=args.row_limit,
            sample_per_db=args.sample_per_db,
            sample_seed=args.sample_seed,
            checkpoint_frequency=args.checkpoint_frequency,
        )
        elapsed = time.time() - start_time
        print(f"Generation complete in {elapsed / 60:.1f} minute(s).")


if __name__ == "__main__":
    main()
