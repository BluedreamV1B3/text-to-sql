# eval_runner.py
r"""
Importable eval runner for text-to-SQL evaluation.

Place this file in the same folder as:
    query_runner.py, metric_manager.py, golden_set_writer.py

Usage in Jupyter
----------------
    from eval_runner import run_eval
    summary = run_eval(excel_path=r"master_results_with_submit_edited.xlsx")

Database layout
---------------
    Insurance   -> Insurance   database, dbo schema   (Windows auth)
    BiomedSQL   -> biomed      database, dbo schema   (SQL auth: biomed_eval)
    The rest    -> kaggleDBQA  database, per-schema   (SQL auth: one login each)

Before first run, execute setup_eval_logins.sql in SSMS to create the logins.

Output structure
----------------
    sql_run_result/
        gpt4o/   <db_id>/   <request_id>.json  + index.csv
        claude/  <db_id>/   ...
        gemini/  <db_id>/   ...
        golden/  <db_id>/   ...   ← ground truth SQL results
"""

import ast
import json
import logging
import os
import re
import time
from queue import Queue
from typing import Dict, Optional

import pandas as pd

try:
    from tqdm.notebook import tqdm
except ImportError:
    from tqdm import tqdm

from golden_set_writer import GoldenSetWriter
from metric_manager import MetricManager
from query_runner import ParallelQueryRunner

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default connection strings
# ---------------------------------------------------------------------------

_SERVER = r".\SQLEXPRESS"
_FLAGS  = "Encrypt=yes;TrustServerCertificate=yes;"
_DRIVER = "ODBC Driver 17 for SQL Server"


def _win(database: str) -> str:
    """Windows auth connection string (used for Insurance)."""
    return (
        f"DRIVER={{{_DRIVER}}};SERVER={_SERVER};DATABASE={database};"
        f"Trusted_Connection=yes;{_FLAGS}"
    )


def _sql(database: str, uid: str, pwd: str = "Eval@2024!") -> str:
    """SQL auth connection string (used for all other databases)."""
    return (
        f"DRIVER={{{_DRIVER}}};SERVER={_SERVER};DATABASE={database};"
        f"UID={uid};PWD={pwd};{_FLAGS}"
    )


DEFAULT_CONNECTION_STRINGS: Dict[str, str] = {
    # Own database, dbo schema, Windows auth
    "Insurance":              _win("Insurance"),
    # Own database, dbo schema, SQL auth
    "BiomedSQL":              _sql("biomed",     "biomed_eval"),
    # kaggleDBQA database, per-schema SQL auth logins
    "GeoNuclearData":         _sql("kaggleDBQA", "geo_eval"),
    "GreaterManchesterCrime": _sql("kaggleDBQA", "gmc_eval"),
    "StudentMathScore":       _sql("kaggleDBQA", "sms_eval"),
    "TheHistoryofBaseball":   _sql("kaggleDBQA", "baseball_eval"),
    "WhatCDHipHop":           _sql("kaggleDBQA", "hiphop_eval"),
    "WorldSoccerDataBase":    _sql("kaggleDBQA", "soccer_eval"),
}

# ---------------------------------------------------------------------------
# Default model columns
# golden_set is the ground truth SQL, others are model-generated
# ---------------------------------------------------------------------------

DEFAULT_MODEL_COLUMNS: Dict[str, str] = {
    "sql_submit_gpt4o":   "gpt4o",
    "sql_submit_claude":  "claude",
    "sql_submit_gemini":  "gemini",
    "golden_set":         "golden",   # ← ground truth
}


# ---------------------------------------------------------------------------
# Parse  runner.submit("SQL...", metadata={...})  cell strings
# ---------------------------------------------------------------------------

_SUBMIT_RE = re.compile(
    r'runner\.submit\s*\(\s*"((?:[^"\\]|\\.)*)"\s*,\s*metadata\s*=\s*(\{.+\})\s*\)\s*$',
    re.DOTALL,
)


def _safe_parse_metadata(raw: str) -> dict:
    """
    Parse a metadata dict string robustly.
    Handles unescaped double-quotes inside string values (common in question text).
    """
    # Fast path
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        pass

    # Sanitise unescaped double-quotes inside the question value
    q_match = re.search(r'"question"\s*:\s*"(.*?)"\s*(?:,\s*"|\})', raw, re.DOTALL)
    if q_match:
        question_val = q_match.group(1)
        placeholder  = "__QUESTION_PLACEHOLDER__"
        sanitised    = raw[: q_match.start(1)] + placeholder + raw[q_match.end(1):]
        try:
            result = ast.literal_eval(sanitised)
            result["question"] = question_val
            return result
        except (ValueError, SyntaxError):
            pass

    # json.loads fallback
    try:
        normalised = re.sub(r"(?<![\\])'", '"', raw)
        return json.loads(normalised)
    except (ValueError, json.JSONDecodeError):
        pass

    raise ValueError(f"Cannot parse metadata: {raw[:200]!r}")


def _parse_submit(cell: str):
    """Return (sql, metadata) from a runner.submit(...) string."""
    m = _SUBMIT_RE.search(cell.strip())
    if not m:
        raise ValueError(f"No runner.submit(...) pattern found: {cell[:120]!r}")
    raw_sql = m.group(1)
    try:
        sql = ast.literal_eval(f'"{raw_sql}"')
    except (ValueError, SyntaxError):
        sql = raw_sql.replace('\\"', '"').replace("\\'", "'")
    metadata = _safe_parse_metadata(m.group(2))
    return sql, metadata


# ---------------------------------------------------------------------------
# Run one (model_col, model_folder, db_id) slice
# ---------------------------------------------------------------------------

def _run_slice(
    model_col: str,
    model_folder: str,
    db_id: str,
    rows: pd.DataFrame,
    conn_str: str,
    base_output_dir: str,
    num_workers: int,
    overwrite_json: bool,
    outer_bar,
) -> dict:
    """Execute all queries for one (model, db_id) pair. Returns a stats dict."""

    out_dir  = os.path.join(base_output_dir, model_folder, db_id)
    csv_path = os.path.join(out_dir, "index.csv")
    os.makedirs(out_dir, exist_ok=True)

    # ── 1. Parse every cell ───────────────────────────────────────────────
    jobs, parse_errors = [], 0

    for _, row in rows.iterrows():
        cell = row[model_col]
        if not isinstance(cell, str) or not cell.strip():
            parse_errors += 1
            continue
        try:
            sql, meta = _parse_submit(cell)
            meta["db_id"]    = db_id
            meta["model"]    = model_folder   # tag which model/source this is
            jobs.append((sql, meta))
        except ValueError as e:
            log.warning("[%s / %s] parse error row=%s: %s",
                        model_folder, db_id, row.get("index", "?"), e)
            parse_errors += 1

    if not jobs:
        msg = "no valid jobs after parsing"
        log.warning("[%s / %s] %s — skipping", model_folder, db_id, msg)
        return {
            "model": model_folder, "db_id": db_id,
            "submitted": 0, "parse_errors": parse_errors,
            "written": 0, "elapsed_s": 0.0,
            "last_error": msg, "out_dir": out_dir,
        }

    # ── 2. Wire up pipeline ───────────────────────────────────────────────
    events_q = Queue()

    mm = MetricManager(
        writer_queue=events_q,
        enable_fact_writes=True,
        enable_metric_writes=False,
    )

    gw = GoldenSetWriter(
        event_queue=events_q,
        out_dir=out_dir,
        index_csv_path=csv_path,
        request_id_key="request_id",
        overwrite_json=overwrite_json,
        flush_every_n=50,
        flush_every_seconds=2.0,
    )
    gw.start()

    runner = ParallelQueryRunner(
        connection_string=conn_str,
        num_workers=num_workers,
        result_callback=mm.add_result,
    )
    runner.start()

    # ── 3. Submit with inner progress bar ─────────────────────────────────
    t0 = time.monotonic()

    inner_bar = tqdm(
        jobs,
        desc=f"  {model_folder:6s} / {db_id}",
        unit="query",
        leave=False,
        colour="green",
    )
    for sql, meta in inner_bar:
        runner.submit(sql, metadata=meta)
        inner_bar.set_postfix(request_id=meta.get("request_id", "?"))
    inner_bar.close()

    # ── 4. Drain and shut down ────────────────────────────────────────────
    runner.stop(drain=True)
    mm.flush_writes()
    gw.stop(drain=True)

    elapsed = time.monotonic() - t0
    stats   = gw.get_stats()

    result = {
        "model":        model_folder,
        "db_id":        db_id,
        "submitted":    len(jobs),
        "parse_errors": parse_errors,
        "written":      stats["events_processed"],
        "elapsed_s":    round(elapsed, 1),
        "last_error":   stats["last_error"] or "",
        "out_dir":      out_dir,
    }

    outer_bar.write(
        f"  {'✓' if not stats['last_error'] else '✗'}"
        f"  {model_folder:6s} / {db_id:<25s}"
        f"  submitted={len(jobs):<4d}"
        f"  written={stats['events_processed']:<4d}"
        f"  parse_errors={parse_errors}"
        f"  errors={stats['last_error'] or 'none'}"
        f"  ({elapsed:.1f}s)"
    )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_eval(
    excel_path: str,
    connection_strings: Optional[Dict[str, str]] = None,
    base_output_dir: str = "sql_run_result",
    num_workers: int = 4,
    overwrite_json: bool = True,
    model_columns: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    """
    Run the full text-to-SQL evaluation loop.

    Parameters
    ----------
    excel_path : str
        Path to master_results_with_submit_edited.xlsx.

    connection_strings : dict[str, str], optional
        Maps each db_id to its ODBC connection string.
        Defaults to DEFAULT_CONNECTION_STRINGS.

    base_output_dir : str
        Root output folder.  Default: "sql_run_result".
        Structure:  <base>/<model>/<db_id>/<request_id>.json
                    <base>/<model>/<db_id>/index.csv

    num_workers : int
        Parallel SQL threads per database.  Default: 4.

    overwrite_json : bool
        Overwrite existing result JSON files.  Default: True.
        Set False to resume a partial run.

    model_columns : dict[str, str], optional
        Maps Excel column name -> output subfolder name.
        Default: gpt4o / claude / gemini / golden (ground truth).

    Returns
    -------
    pd.DataFrame
        One row per (model, db_id) with submitted/written/elapsed stats.
    """

    # ── Lazy logging setup ────────────────────────────────────────────────
    if not logging.root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%H:%M:%S",
        )

    if connection_strings is None:
        connection_strings = DEFAULT_CONNECTION_STRINGS

    if model_columns is None:
        model_columns = DEFAULT_MODEL_COLUMNS

    # ── Load Excel ────────────────────────────────────────────────────────
    log.info("Loading %s ...", excel_path)
    df = pd.read_excel(excel_path)
    df["db_id"] = df["db_id"].astype(str).str.strip()

    unique_dbs = df["db_id"].unique().tolist()
    log.info("Loaded %d rows  |  databases: %s", len(df), unique_dbs)

    missing_cs = [d for d in unique_dbs if d not in connection_strings]
    if missing_cs:
        log.warning("No connection string for %s — those rows will be skipped.", missing_cs)

    # ── Build combo list ──────────────────────────────────────────────────
    combos = [
        (model_col, model_folder, db_id, group)
        for model_col, model_folder in model_columns.items()
        for db_id, group in df.groupby("db_id")
        if db_id in connection_strings
    ]

    total_queries = sum(len(g) for _, _, _, g in combos)
    log.info("%d model x db combos  |  ~%d total query executions",
             len(combos), total_queries)

    # ── Outer progress bar ────────────────────────────────────────────────
    run_log   = []
    t0_global = time.monotonic()

    outer_bar = tqdm(combos, desc="Overall", unit="batch", colour="blue")

    for model_col, model_folder, db_id, group in outer_bar:
        outer_bar.set_description(f"Overall  [{model_folder} / {db_id}]")

        try:
            entry = _run_slice(
                model_col=model_col,
                model_folder=model_folder,
                db_id=db_id,
                rows=group,
                conn_str=connection_strings[db_id],
                base_output_dir=base_output_dir,
                num_workers=num_workers,
                overwrite_json=overwrite_json,
                outer_bar=outer_bar,
            )
        except Exception as e:
            log.error("FATAL  [%s / %s]: %s", model_folder, db_id, e, exc_info=True)
            entry = {
                "model": model_folder, "db_id": db_id,
                "submitted": 0, "parse_errors": 0, "written": 0,
                "elapsed_s": 0.0, "last_error": str(e), "out_dir": "",
            }

        run_log.append(entry)

    outer_bar.close()
    total_elapsed = time.monotonic() - t0_global

    # ── Final summary ─────────────────────────────────────────────────────
    summary = pd.DataFrame(run_log)
    cols    = ["model", "db_id", "submitted", "written", "parse_errors", "elapsed_s", "last_error"]

    print(f"\n{'=' * 68}")
    print(f"  EVAL COMPLETE   total time: {total_elapsed:.1f}s")
    print(f"{'=' * 68}")
    print(summary[cols].to_string(index=False))

    failed = summary[summary["last_error"] != ""]
    print(f"\n  Total submitted : {summary['submitted'].sum()}")
    print(f"  Total written   : {summary['written'].sum()}")
    print(f"  Failed batches  : {len(failed)}")

    if not failed.empty:
        print("\n  Batches with errors:")
        print(failed[["model", "db_id", "last_error"]].to_string(index=False))

    print(f"\n  Results saved to: {os.path.abspath(base_output_dir)}{os.sep}")

    return summary
