import os
import sys
import json
import importlib
import pandas as pd
from copy import deepcopy
from pathlib import Path
import traceback
import warnings
import time

warnings.filterwarnings("ignore")


# =========================================================
# Setup & Configuration
# =========================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATION_DIR = Path(__file__).resolve().parent

for path in (PROJECT_ROOT, GENERATION_DIR):
    if str(path) not in sys.path:
        sys.path.append(str(path))

from configuration import get_configuration
from LLMFactory import create_llm

OUTPUT_DIR = Path(
    os.getenv(
        "TEXT2SQL_GENERATION_OUTPUT_DIR",
        PROJECT_ROOT / "artifacts" / "generation_outputs",
    )
)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Timing / retry constants
MODEL_SLEEP_SECONDS     = 2      # Sleep between model calls within a question
CHECKPOINT_FREQUENCY    = 3      # Save checkpoint every N questions
COOLDOWN_SECONDS        = 120    # Cooldown between checkpoint batches (seconds)
MAX_RETRIES             = 3      # Max retries for GPT-4o / Gemini

# Claude-specific rate-limit constants.
# Override CLAUDE_MIN_INTERVAL_SECONDS from PowerShell if your Anthropic tier allows
# more than 5 RPM. Example: $env:CLAUDE_MIN_INTERVAL_SECONDS="6"
CLAUDE_MIN_INTERVAL       = float(os.getenv("CLAUDE_MIN_INTERVAL_SECONDS", "12"))
CLAUDE_MAX_TOTAL_RETRIES  = int(os.getenv("CLAUDE_MAX_TOTAL_RETRIES", "10"))

# Global tracker for Claude throttling
_last_claude_call_time: float = 0.0


def repo_path(*parts: str) -> str:
    return str(PROJECT_ROOT.joinpath(*parts))


# =========================================================
# Database Registry
# =========================================================

DATABASES = [
    {
        "db_id": "Insurance",
        "schema_path":   repo_path("Insurance", "schema_insurance.py"),
        "question_path": repo_path("Insurance", "question_insurance.py"),
    },
    {
        "db_id": "GeoNuclearData",
        "schema_path":   repo_path("KaggleDBQA", "schemas", "schema_GeoNuclearData.py"),
        "question_path": repo_path("KaggleDBQA", "questions", "question_GeoNuclearData.py"),
    },
    {
        "db_id": "GreaterManchesterCrime",
        "schema_path":   repo_path("KaggleDBQA", "schemas", "schema_GreaterManchesterCrime.py"),
        "question_path": repo_path("KaggleDBQA", "questions", "question_GreaterManchesterCrime.py"),
    },
    {
        "db_id": "StudentMathScore",
        "schema_path":   repo_path("KaggleDBQA", "schemas", "schema_StudentMathScore.py"),
        "question_path": repo_path("KaggleDBQA", "questions", "question_StudentMathScore.py"),
    },
    {
        "db_id": "TheHistoryofBaseball",
        "schema_path":   repo_path("KaggleDBQA", "schemas", "schema_TheHistoryofBaseball.py"),
        "question_path": repo_path("KaggleDBQA", "questions", "question_TheHistoryofBaseball.py"),
    },
    {
        "db_id": "USWildFires",
        "schema_path":   repo_path("KaggleDBQA", "schemas", "schema_USWildFires.py"),
        "question_path": repo_path("KaggleDBQA", "questions", "question_USWildFires.py"),
    },
    {
        "db_id": "WhatCDHipHop",
        "schema_path":   repo_path("KaggleDBQA", "schemas", "schema_WhatCDHipHop.py"),
        "question_path": repo_path("KaggleDBQA", "questions", "question_WhatCDHipHop.py"),
    },
    {
        "db_id": "WorldSoccerDataBase",
        "schema_path":   repo_path("KaggleDBQA", "schemas", "schema_WorldSoccerDataBase.py"),
        "question_path": repo_path("KaggleDBQA", "questions", "question_WorldSoccerDataBase.py"),
    },
    {
        "db_id": "BiomedSQL",
        "schema_path":   repo_path("BiomedSQL", "schema_biomed.py"),
        "question_path": repo_path("BiomedSQL", "question_biomed.py"),
    },
]


# =========================================================
# Schema & Question Loaders
# =========================================================

def load_schema(schema_path: str) -> dict:
    """Dynamically import a schema module and return its databaseSchema dict."""
    module_dir  = os.path.dirname(schema_path)
    module_name = os.path.splitext(os.path.basename(schema_path))[0]

    if module_dir not in sys.path:
        sys.path.append(module_dir)

    module = importlib.import_module(module_name)
    module = importlib.reload(module)
    return module.databaseSchema


def load_questions(question_path: str) -> list:
    """Load questions from a .py module or a .json file."""
    if question_path.endswith(".py"):
        module_dir  = os.path.dirname(question_path)
        module_name = os.path.splitext(os.path.basename(question_path))[0]

        if module_dir not in sys.path:
            sys.path.append(module_dir)

        module = importlib.import_module(module_name)
        module = importlib.reload(module)
        return module.questions

    elif question_path.endswith(".json"):
        with open(question_path, "r", encoding="utf-8") as f:
            return json.load(f)

    else:
        raise ValueError(f"Unsupported question file format: {question_path}")


def normalize_question(question) -> str:
    """
    Ensure a question is always a plain string.
    JSON-loaded questions may arrive as dicts; extract the text field if so.
    """
    if isinstance(question, dict):
        for key in ("question", "text", "query", "nl"):
            if key in question:
                return question[key]
        print(f"⚠ Warning: unknown question dict keys: {list(question.keys())}")
        return str(question)
    return str(question)


# =========================================================
# Prompt Builder
# =========================================================

def build_system_prompt(base_prompt: str, schema_dict: dict) -> str:
    """Append a JSON-serialised schema to the base system prompt."""
    schema_json = json.dumps(schema_dict, indent=2)
    return f"{base_prompt}\n\nSCHEMA JSON:\n{schema_json}"


# =========================================================
# Checkpoint Utilities
# =========================================================

def save_checkpoint(df: pd.DataFrame, db_id: str, question_idx: int) -> None:
    filename = f"{db_id}_checkpoint_{question_idx}.xlsx"
    path     = os.path.join(OUTPUT_DIR, filename)
    df.to_excel(path, index=False)
    print(f"\n💾 Checkpoint saved: {filename}")

    for f in os.listdir(OUTPUT_DIR):
        if (
            f.startswith(f"{db_id}_checkpoint_")
            and f.endswith(".xlsx")
            and f != filename
        ):
            os.remove(os.path.join(OUTPUT_DIR, f))
            print(f"🗑  Deleted old checkpoint: {f}")


def get_last_checkpoint(db_id: str) -> tuple[int, list]:
    """Return (start_index, existing_rows) from the latest checkpoint, or (0, [])."""
    checkpoints = [
        f for f in os.listdir(OUTPUT_DIR)
        if f.startswith(f"{db_id}_checkpoint_") and f.endswith(".xlsx")
    ]

    if not checkpoints:
        return 0, []

    checkpoints.sort(key=lambda x: int(x.split("_")[-1].split(".")[0]))
    latest = checkpoints[-1]
    df     = pd.read_excel(os.path.join(OUTPUT_DIR, latest))

    print(f"🔁 Resuming from {latest} ({len(df)} rows already saved)")
    return len(df), df.to_dict("records")


# =========================================================
# LLM Initialisation
# =========================================================

def initialize_llms() -> dict:
    """
    Instantiate all three model clients and return them in a dict.
    'base_prompt' is stored separately as an immutable snapshot so that
    schema injection across databases never accumulates stale schemas.
    """
    llms = {}
    for model_name in ("gpt4o", "claude", "gemini"):
        config      = deepcopy(get_configuration(model_name))
        llm         = create_llm(model_name, config)
        llms[model_name] = {
            "config":      config,
            "llm":         llm,
            "base_prompt": config["prompt"]["system"],  # ← immutable snapshot
        }
    return llms


# =========================================================
# Model Call Helpers
# =========================================================

def _call_gpt_or_gemini(llm, question: str, model_name: str) -> dict:
    """Simple retry loop for GPT-4o and Gemini."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = llm.getResponseFunction(question)
            if isinstance(result, dict) and result.get("sql"):
                return result
            print(f"    ⚠ {model_name} attempt {attempt}/{MAX_RETRIES}: empty result, retrying…")
        except Exception as e:
            error_msg = str(e)
            print(f"    ⚠ {model_name} attempt {attempt}/{MAX_RETRIES}: {error_msg[:100]}")
            if "429" in error_msg or "RateLimit" in error_msg:
                print(f"    ⏳ Rate limit detected — cooling 30s…")
                time.sleep(30)
            else:
                time.sleep(3)

    return {"sql": "", "explanation": f"{model_name.upper()}_FAILED_AFTER_{MAX_RETRIES}_RETRIES"}


def _call_claude(llm, question: str) -> dict:
    """Rate-limit-safe Claude caller."""
    global _last_claude_call_time

    for attempt in range(1, CLAUDE_MAX_TOTAL_RETRIES + 1):

        elapsed = time.time() - _last_claude_call_time
        if elapsed < CLAUDE_MIN_INTERVAL:
            wait = CLAUDE_MIN_INTERVAL - elapsed
            print(f"    ⏳ Throttling: waiting {wait:.1f}s to respect Claude min interval…")
            time.sleep(wait)

        _last_claude_call_time = time.time()

        try:
            result = llm.getResponseFunction(question)
            if isinstance(result, dict) and result.get("sql"):
                return result
            print(f"    ⚠ Claude attempt {attempt}/{CLAUDE_MAX_TOTAL_RETRIES}: empty result, retrying…")

        except Exception as e:
            error_msg = str(e)
            print(f"    ⚠ Claude attempt {attempt}/{CLAUDE_MAX_TOTAL_RETRIES}: {error_msg[:120]}")

            lower_error = error_msg.lower()
            if "credit balance is too low" in lower_error:
                print("    ❌ Claude billing/credit error detected. Stopping retries for this question.")
                break
            if "429" in error_msg or "ratelimit" in lower_error or "rate limit" in lower_error or "overloaded" in lower_error:
                print("    ⏳ Rate limit / overload detected — cooling 60s…")
                time.sleep(60)
            else:
                time.sleep(5)

    print("    ❌ Claude exceeded max retries. Skipping question.")
    return {"sql": "", "explanation": "CLAUDE_MAX_RETRIES_EXCEEDED"}


def safe_model_call(llm, question: str, model_name: str) -> dict:
    """Dispatch to the appropriate caller based on model name."""
    if model_name == "claude":
        return _call_claude(llm, question)
    return _call_gpt_or_gemini(llm, question, model_name)


def _needs_retry(val) -> bool:
    """True when a SQL cell is empty or contains a failure sentinel."""
    if pd.isna(val):
        return True
    s = str(val).strip()
    if s in ("", "nan", "None"):
        return True
    return any(tok in s.upper() for tok in ("FAILED", "EXCEEDED", "ERROR", "PARSE"))


# =========================================================
# Main Experiment Loop
# =========================================================

def run_experiment(llm_pool: dict) -> None:
    for db_info in DATABASES:
        db_id = db_info["db_id"]

        print("\n" + "=" * 80)
        print(f"DATABASE: {db_id}")
        print("=" * 80)

        schema    = load_schema(db_info["schema_path"])
        questions = load_questions(db_info["question_path"])

        start_index, rows = get_last_checkpoint(db_id)
        total = len(questions)

        print(f"Loaded {total} questions — starting from question {start_index + 1}")

        for idx in range(start_index, total):
            question_raw    = questions[idx]
            question_text   = normalize_question(question_raw)
            question_number = idx + 1

            print(f"\n[{question_number}/{total}] {question_text[:80]}…")

            row = {
                "id":       question_number,
                "db_id":    db_id,
                "question": question_text,
            }

            for model_name in ("gpt4o", "claude", "gemini"):
                model_info  = llm_pool[model_name]
                llm         = model_info["llm"]

                # Use the immutable base_prompt snapshot — never reads back from
                # llm.configuration so the schema never accumulates across databases.
                llm.configuration["prompt"]["system"] = build_system_prompt(
                    model_info["base_prompt"], schema
                )

                time.sleep(MODEL_SLEEP_SECONDS)

                response = safe_model_call(llm, question_text, model_name)

                row[f"sql_{model_name}"]         = response.get("sql", "")
                row[f"explanation_{model_name}"] = response.get("explanation", "")

                print(f"  ✓ {model_name}")

            rows.append(row)

            is_checkpoint = (
                question_number % CHECKPOINT_FREQUENCY == 0
                or question_number == total
            )

            if is_checkpoint:
                save_checkpoint(pd.DataFrame(rows), db_id, question_number)

                if question_number < total:
                    print(f"⏳ Cooldown: sleeping {COOLDOWN_SECONDS}s before next batch…")
                    time.sleep(COOLDOWN_SECONDS)

        final_path = os.path.join(OUTPUT_DIR, f"{db_id}.xlsx")
        pd.DataFrame(rows).to_excel(final_path, index=False)
        print(f"\n✅ Completed: {db_id}  →  {final_path}")

    print("\n🎉 ALL DATABASES COMPLETE")


# =========================================================
# Retry Empty SQL
# =========================================================

MAX_RETRY_PASSES = 5  # Maximum full passes per database before giving up

def retry_empty_sql(llm_pool: dict) -> None:
    """
    Scan all per-database output files for empty or failed SQL cells,
    rerun only those questions against the appropriate model(s), and
    write the filled results back into the same .xlsx files in-place.

    Keeps retrying pass-by-pass until all empty cells are filled or
    MAX_RETRY_PASSES is reached with no progress.
    """
    print("\n" + "=" * 80)
    print("RETRY PASS: scanning for empty / failed SQL")
    print("=" * 80)

    for db_info in DATABASES:
        db_id     = db_info["db_id"]
        xlsx_path = os.path.join(OUTPUT_DIR, f"{db_id}.xlsx")

        if not os.path.exists(xlsx_path):
            print(f"  ⚠  {db_id}: output file not found, skipping.")
            continue

        df = pd.read_excel(xlsx_path)

        pass_number   = 0
        total_fixed   = 0

        while True:
            # ── Find all cells that still need retrying ──────────────────
            to_retry = []
            for m in ("gpt4o", "claude", "gemini"):
                sql_col = f"sql_{m}"
                if sql_col not in df.columns:
                    continue
                for row_idx in df.index[df[sql_col].apply(_needs_retry)]:
                    to_retry.append((row_idx, m))

            if not to_retry:
                if pass_number == 0:
                    print(f"  ✓  {db_id}: no empty SQL found.")
                else:
                    print(f"  ✅ {db_id}: all cells filled after {pass_number} pass(es).")
                break

            pass_number += 1
            if pass_number > MAX_RETRY_PASSES:
                remaining = len(to_retry)
                print(f"  ⚠  {db_id}: reached MAX_RETRY_PASSES ({MAX_RETRY_PASSES}) "
                      f"with {remaining} cell(s) still empty — giving up.")
                break

            models_affected = sorted({m for _, m in to_retry})
            print(f"\n  {db_id} — pass {pass_number}/{MAX_RETRY_PASSES}: "
                  f"{len(to_retry)} cell(s) to retry — models: {models_affected}")

            # ── Inject schema once per pass ──────────────────────────────
            schema = load_schema(db_info["schema_path"])
            for m in models_affected:
                info = llm_pool[m]
                info["llm"].configuration["prompt"]["system"] = build_system_prompt(
                    info["base_prompt"], schema
                )

            # ── Retry each empty cell ────────────────────────────────────
            n_fixed_this_pass = 0
            for row_idx, m in to_retry:
                question = str(df.at[row_idx, "question"])
                print(f"    [{row_idx}] {m:10s}  q: {question[:70]}…")

                response = safe_model_call(llm_pool[m]["llm"], question, m)
                new_sql  = response.get("sql", "")
                new_exp  = response.get("explanation", "")

                if new_sql and not _needs_retry(new_sql):
                    df.at[row_idx, f"sql_{m}"]         = new_sql
                    df.at[row_idx, f"explanation_{m}"] = new_exp
                    n_fixed_this_pass += 1
                    total_fixed       += 1
                    print(f"      ✓ filled ({len(new_sql)} chars)")
                else:
                    print(f"      ✗ still empty after retry")

            # ── Save after every pass that made progress ─────────────────
            if n_fixed_this_pass > 0:
                df.to_excel(xlsx_path, index=False)
                print(f"  💾 {db_id}: {n_fixed_this_pass} cell(s) filled this pass "
                      f"({total_fixed} total) — file updated.")
            else:
                # No progress this pass — stop early to avoid pointless retries
                remaining = len(to_retry)
                print(f"  ⚠  {db_id}: no progress in pass {pass_number} "
                      f"({remaining} cell(s) still empty) — stopping early.")
                break

    print("\n✅ Retry pass complete.")


# =========================================================
# Entry Point
# =========================================================

if __name__ == "__main__":
    try:
        llm_pool = initialize_llms()
        #run_experiment(llm_pool)
        retry_empty_sql(llm_pool)
    except KeyboardInterrupt:
        print("\n⚠ Interrupted by user — progress saved at last checkpoint.")
    except Exception:
        print("\n❌ Fatal error:")
        traceback.print_exc()
