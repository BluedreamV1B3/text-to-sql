from pathlib import Path
import sys

import eval_runner
from eval_runner import DEFAULT_CONNECTION_STRINGS, run_eval


class QuietTqdm:
    """Minimal tqdm-compatible wrapper for non-interactive benchmark runs."""

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


EXCEL_PATH = "master_results_final.xlsx"
OUTPUT_DIR = "sql_run_result_final"
SUMMARY_CSV = "sql_run_result_final_summary.csv"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    Path(OUTPUT_DIR).mkdir(exist_ok=True)

    # Run quietly outside a live notebook and avoid tqdm.notebook/console
    # encoding issues.
    eval_runner.tqdm = QuietTqdm

    summary = run_eval(
        excel_path=EXCEL_PATH,
        connection_strings=DEFAULT_CONNECTION_STRINGS,
        base_output_dir=OUTPUT_DIR,
        num_workers=4,
        overwrite_json=True,
    )
    summary.to_csv(SUMMARY_CSV, index=False)
    print(f"\nFinal benchmark summary saved to: {Path(SUMMARY_CSV).resolve()}")


if __name__ == "__main__":
    main()
