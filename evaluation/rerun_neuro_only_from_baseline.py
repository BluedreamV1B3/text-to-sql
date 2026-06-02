import csv
import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

import pandas as pd

import eval_runner
from eval_runner import DEFAULT_CONNECTION_STRINGS, _parse_submit, run_eval


BASELINE_SUMMARY = Path("final_result_files/full_benchmark_results_summary_gold_corrected.xlsx")
MASTER_WORKBOOK = (
    Path("master_results_gold_corrected.xlsx")
    if Path("master_results_gold_corrected.xlsx").exists()
    else Path("final_result_files/master_results_gold_corrected.xlsx")
)
RESULT_DIR = Path("sql_run_result_final")
RERUN_DIR = Path("final_result_files/neuro_only_rerun")
RERUN_SUMMARY_CSV = Path("final_result_files/rerun_neuro_only_summary.csv")

NEURO_ERROR = "Invalid object name 'NeurodegenerativeDiseases_SMR_Genes_Full'"
DRUG_TABLE = "DrugTargets_IndicationsAndTherapeuticUses"

MODEL_TO_COLUMN = {
    "gpt4o": "sql_submit_gpt4o",
    "claude": "sql_submit_claude",
    "gemini": "sql_submit_gemini",
}


class QuietTqdm:
    """Minimal tqdm-compatible wrapper for non-interactive reruns."""

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


def clean_scalar(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def read_rows_cell(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    try:
        if pd.isna(value):
            return []
    except Exception:
        pass
    return json.loads(str(value))


def load_json_rows(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        rec = json.load(f)
    rows = (rec.get("result") or {}).get("rows")
    return rows if isinstance(rows, list) else []


def source_json_path(source: str, db_id: str, request_id: int) -> Path:
    return RESULT_DIR / source / db_id / f"{request_id}.json"


def observed_error(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        return ""
    return str((rec.get("observed") or {}).get("error") or "")


def metadata_from_master(master_by_id: pd.DataFrame, request_id: int, source: str) -> Dict[str, Any]:
    row = master_by_id.loc[request_id]
    column = "golden_set" if source == "golden" else MODEL_TO_COLUMN[source]
    _, metadata = _parse_submit(str(row[column]))
    metadata["request_id"] = request_id
    metadata["model"] = source
    metadata["db_id"] = str(row["db_id"])
    metadata["question"] = str(row["question"])
    return metadata


def baseline_row(summary: pd.DataFrame, request_id: int, source: str) -> pd.Series:
    if source == "golden":
        rows = summary[summary["request_id"].astype(int).eq(request_id)]
    else:
        rows = summary[
            summary["request_id"].astype(int).eq(request_id)
            & summary["model"].astype(str).eq(source)
        ]
    if rows.empty:
        raise KeyError(f"No baseline row for source={source} request_id={request_id}")
    return rows.iloc[0]


def rows_for_baseline_record(row: pd.Series, source: str) -> List[Dict[str, Any]]:
    cell = row["golden_result_rows"] if source == "golden" else row["model_result_rows"]
    try:
        return read_rows_cell(cell)
    except Exception:
        # Excel-truncated golden rows can often be recovered from golden_old if
        # the SQL is identical to the baseline SQL.
        if source == "golden":
            request_id = int(row["request_id"])
            db_id = str(row["db_id"])
            old_path = source_json_path("golden_old", db_id, request_id)
            if old_path.exists():
                with old_path.open("r", encoding="utf-8") as f:
                    old_rec = json.load(f)
                if str(old_rec.get("sql") or "").strip() == str(row["golden_sql"]).strip():
                    return load_json_rows(old_path)
        raise


def restore_source_from_baseline(
    summary: pd.DataFrame,
    master_by_id: pd.DataFrame,
    request_id: int,
    source: str,
    output_source: str | None = None,
) -> None:
    output_source = output_source or source
    row = baseline_row(summary, request_id, "golden" if source == "golden" else source)
    db_id = str(row["db_id"])
    rows = rows_for_baseline_record(row, source)

    if source == "golden":
        ok = bool(row["golden_ok"])
        error = clean_scalar(row["golden_error"])
        row_count = clean_scalar(row["golden_row_count_observed"])
        cpu_ms = clean_scalar(row["golden_server_cpu_ms_observed"])
        elapsed_ms = clean_scalar(row["golden_server_elapsed_ms_observed"])
        sql = str(row["golden_sql"])
    else:
        ok = bool(row["model_ok"])
        error = clean_scalar(row["model_error"])
        row_count = clean_scalar(row["model_row_count"])
        cpu_ms = clean_scalar(row["model_server_cpu_ms"])
        elapsed_ms = clean_scalar(row["model_server_elapsed_ms"])
        sql = str(row["model_sql"])

    metadata = metadata_from_master(master_by_id, request_id, source)
    record = {
        "request_id": request_id,
        "question": str(row["question"]),
        "run_id": "baseline_restore_before_neuro_only_rerun",
        "run_label": None,
        "job_id": f"baseline-restore-{output_source}-{request_id}",
        "sql": sql,
        "params_json": None,
        "metadata": metadata,
        "observed": {
            "ok": ok,
            "error": error,
            "row_count": row_count,
            "server_cpu_ms": cpu_ms,
            "server_elapsed_ms": elapsed_ms,
        },
        "result": {
            "rows": rows,
        },
    }

    out_path = source_json_path(output_source, db_id, request_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, default=str, indent=2)


def restore_drug_overwrites(summary: pd.DataFrame, master_by_id: pd.DataFrame) -> Dict[str, int]:
    restored = {"golden": 0, "golden_new": 0, "gpt4o": 0, "claude": 0, "gemini": 0}
    for source in ["golden", "golden_new", "gpt4o", "claude", "gemini"]:
        scan_source = "golden" if source == "golden_new" else source
        for path in sorted((RESULT_DIR / source / "BiomedSQL").glob("*.json")):
            if DRUG_TABLE not in observed_error(path):
                continue
            request_id = int(path.stem)
            restore_source_from_baseline(
                summary=summary,
                master_by_id=master_by_id,
                request_id=request_id,
                source=scan_source,
                output_source=source,
            )
            restored[source] += 1
    return restored


def build_rerun_plan(summary: pd.DataFrame) -> tuple[Set[int], Dict[str, Set[int]], List[Dict[str, Any]]]:
    skipped: List[Dict[str, Any]] = []

    golden_mask = summary["golden_error"].fillna("").astype(str).str.contains(NEURO_ERROR, regex=False)
    golden_ids: Set[int] = set()
    for request_id, group in summary[golden_mask].groupby("request_id"):
        row = group.iloc[0]
        if DRUG_TABLE in str(row["golden_sql"]):
            skipped.append({"source": "golden", "request_id": int(request_id), "reason": "golden_sql_references_deleted_drugtargets"})
        else:
            golden_ids.add(int(request_id))

    model_ids: Dict[str, Set[int]] = {model: set() for model in MODEL_TO_COLUMN}
    for _, row in summary.iterrows():
        request_id = int(row["request_id"])
        model = str(row["model"])
        model_sql = str(row["model_sql"])
        has_model_neuro_error = NEURO_ERROR in str(row.get("model_error") or "")
        should_rerun_because_golden_failed = request_id in golden_ids
        if not (has_model_neuro_error or should_rerun_because_golden_failed):
            continue
        if DRUG_TABLE in model_sql:
            skipped.append({"source": model, "request_id": request_id, "reason": "model_sql_references_deleted_drugtargets"})
            continue
        model_ids[model].add(request_id)

    return golden_ids, model_ids, skipped


def write_source_workbook(master: pd.DataFrame, request_ids: Iterable[int], source: str) -> Path:
    ids = sorted(set(int(x) for x in request_ids))
    if not ids:
        raise ValueError(f"No request IDs for {source}")
    subset = master[master["index"].astype(int).isin(ids)].copy()
    subset = subset.sort_values("index").reset_index(drop=True)
    missing = sorted(set(ids) - set(subset["index"].astype(int)))
    if missing:
        raise ValueError(f"Missing request IDs for {source}: {missing}")
    RERUN_DIR.mkdir(parents=True, exist_ok=True)
    path = RERUN_DIR / f"{source}_rerun.xlsx"
    subset.to_excel(path, index=False)
    return path


def run_source(source: str, workbook_path: Path) -> pd.DataFrame:
    model_columns = {"golden_set": "golden"} if source == "golden" else {MODEL_TO_COLUMN[source]: source}
    return run_eval(
        excel_path=str(workbook_path),
        connection_strings=DEFAULT_CONNECTION_STRINGS,
        base_output_dir=str(RESULT_DIR),
        num_workers=4,
        overwrite_json=True,
        model_columns=model_columns,
    )


def mirror_golden_to_golden_new(request_ids: Iterable[int]) -> None:
    for request_id in sorted(set(int(x) for x in request_ids)):
        for db_dir in (RESULT_DIR / "golden").iterdir():
            if not db_dir.is_dir():
                continue
            src = db_dir / f"{request_id}.json"
            if src.exists():
                target_dir = RESULT_DIR / "golden_new" / db_dir.name
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target_dir / src.name)


def rebuild_index_csv(folder: str, db_id: str = "BiomedSQL") -> None:
    db_dir = RESULT_DIR / folder / db_id
    if not db_dir.exists():
        return

    rows = []
    for path in sorted(db_dir.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else p.stem):
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


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    summary = pd.read_excel(BASELINE_SUMMARY, sheet_name="full_benchmark_summary")
    master = pd.read_excel(MASTER_WORKBOOK)
    master["index"] = master["index"].astype(int)
    master_by_id = master.set_index("index", drop=False)

    restored = restore_drug_overwrites(summary, master_by_id)
    print("Restored current DrugTargets-overwritten JSONs from baseline workbook:")
    print(restored)

    golden_ids, model_ids, skipped = build_rerun_plan(summary)
    print(f"Golden rerun IDs ({len(golden_ids)}): {sorted(golden_ids)}")
    for model, ids in model_ids.items():
        print(f"{model} rerun IDs ({len(ids)}): {sorted(ids)}")
    print(f"Skipped deleted-DrugTargets source-query pairs: {len(skipped)}")
    if skipped:
        RERUN_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(skipped).to_csv(RERUN_DIR / "skipped_deleted_drugtargets_pairs.csv", index=False)

    eval_runner.tqdm = QuietTqdm
    summaries = []

    if golden_ids:
        wb = write_source_workbook(master, golden_ids, "golden")
        summaries.append(run_source("golden", wb))
        mirror_golden_to_golden_new(golden_ids)

    for model, ids in model_ids.items():
        if not ids:
            continue
        wb = write_source_workbook(master, ids, model)
        summaries.append(run_source(model, wb))

    for folder in ["golden", "golden_new", "gpt4o", "claude", "gemini"]:
        rebuild_index_csv(folder)

    if summaries:
        pd.concat(summaries, ignore_index=True).to_csv(RERUN_SUMMARY_CSV, index=False)
    else:
        pd.DataFrame().to_csv(RERUN_SUMMARY_CSV, index=False)
    print(f"Rerun summary saved to: {RERUN_SUMMARY_CSV.resolve()}")


if __name__ == "__main__":
    main()
