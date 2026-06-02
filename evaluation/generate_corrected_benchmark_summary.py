from pathlib import Path

import generate_full_benchmark_summary as base


base.RESULT_DIR = Path("sql_run_result_final")
for candidate in [
    Path("master_results_gold_corrected.xlsx"),
    Path("final_result_files/master_results_gold_corrected.xlsx"),
    Path("final_result_files/master_results.xlsx"),
]:
    if candidate.exists():
        base.MASTER_WORKBOOK = candidate
        break
else:
    base.MASTER_WORKBOOK = Path("final_result_files/master_results.xlsx")

base.OUT_XLSX = Path("full_benchmark_results.xlsx")


if __name__ == "__main__":
    base.main()
