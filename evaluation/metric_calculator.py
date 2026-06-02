# metric_calculator.py
import json
import hashlib
from typing import Any, Dict, List, Optional


class MetricCalculator:
    """
    Computes metrics by comparing QueryResult to a GoldenSet.

    Assumes GoldenSet supports:
      - load_rows_for_result(result) -> list[dict]
      - load_record(request_id) OR load_record_for_result(result) (see below)
    """

    def __init__(self, golden_set):
        self.golden_set = golden_set

    # -------------------------
    # Accuracy vs golden rows
    # -------------------------

    def accuracy_vs_golden(self, result: Any) -> Dict[str, Any]:
        golden_rows = self.golden_set.load_rows_for_result(result)
        actual_rows = getattr(result, "rows", None) or []
        return self._compare_rows(actual_rows, golden_rows)

    # -------------------------
    # Runtime delta vs golden
    # -------------------------

    def runtime_delta_vs_golden(self, result: Any) -> Dict[str, Any]:
        """
        Compare server_elapsed_ms for this result to the golden observed server_elapsed_ms.

        Uses the same golden resolution key (request_id) as GoldenSet.load_rows_for_result.
        """
        golden_record = self._load_golden_record_for_result(result)

        golden_elapsed = self._safe_float(((golden_record.get("observed") or {}).get("server_elapsed_ms")))
        actual_elapsed = self._safe_float(getattr(result, "server_elapsed_ms", None))

        # If either missing, emit explicit missing flags
        if golden_elapsed is None or actual_elapsed is None:
            return {
                "golden_elapsed_ms": golden_elapsed,
                "actual_elapsed_ms": actual_elapsed,
                "runtime_delta_ms": None,
                "runtime_delta_pct": None,
                "runtime_missing": 1.0,
            }

        delta_ms = actual_elapsed - golden_elapsed
        delta_pct = (delta_ms / golden_elapsed) if golden_elapsed != 0 else None

        return {
            "golden_elapsed_ms": golden_elapsed,
            "actual_elapsed_ms": actual_elapsed,
            "runtime_delta_ms": delta_ms,
            "runtime_delta_pct": delta_pct,
            "runtime_missing": 0.0,
        }

    def _load_golden_record_for_result(self, result: Any) -> Dict[str, Any]:
        """
        Tries to load the full golden record for this result.

        Preferred: GoldenSet.load_record_for_result(result)
        Fallback: resolve request_id and call GoldenSet.load_record(request_id)
        """
        # Preferred API if you add it to GoldenSet
        if hasattr(self.golden_set, "load_record_for_result"):
            return self.golden_set.load_record_for_result(result)

        # Fallback: derive request_id the same way GoldenSet does
        job = getattr(result, "job", None)
        md = getattr(job, "metadata", None)
        if not isinstance(md, dict):
            raise KeyError("QueryResult.job.metadata is missing or not a dict")

        request_id_key = getattr(self.golden_set, "request_id_key", "request_id")
        request_id = md.get(request_id_key)
        if not request_id:
            raise KeyError(f"metadata['{request_id_key}'] is missing")

        if not hasattr(self.golden_set, "load_record"):
            raise AttributeError("GoldenSet must implement load_record(request_id) or load_record_for_result(result)")

        return self.golden_set.load_record(str(request_id))

    # -------------------------
    # Row comparison helpers
    # -------------------------

    def _compare_rows(
        self,
        actual_rows: List[Dict[str, Any]],
        golden_rows: List[Dict[str, Any]],
    ) -> Dict[str, Any]:

        actual_count = len(actual_rows)
        golden_count = len(golden_rows)

        actual_hash = self._hash_rows(actual_rows)
        golden_hash = self._hash_rows(golden_rows)

        exact_match = (actual_hash == golden_hash)

        return {
            "golden_row_count": golden_count,
            "actual_row_count": actual_count,
            "row_count_match": 1.0 if actual_count == golden_count else 0.0,
            "exact_match": 1.0 if exact_match else 0.0,
            "golden_rows_hash": golden_hash,
            "actual_rows_hash": actual_hash,
        }

    def _hash_rows(self, rows: List[Dict[str, Any]]) -> str:
        h = hashlib.sha256()

        canonical = []
        for row in rows:
            if isinstance(row, dict):
                canonical.append(self._canonical_row(row))
            else:
                canonical.append(json.dumps(row, sort_keys=True, default=str))

        for s in sorted(canonical):
            h.update(s.encode("utf-8"))
            h.update(b"\n")

        return "sha256:" + h.hexdigest()

    def _canonical_row(self, row: Dict[str, Any]) -> str:
        """
        Compare result rows by values only.

        SQL aliases and output column ordering should not affect accuracy, so a
        row is represented as the sorted multiset of its cell values.
        """
        values = [
            None if value is None else str(value)
            for value in row.values()
        ]
        return json.dumps(sorted(values, key=lambda v: "" if v is None else v), ensure_ascii=False, separators=(",", ":"))

    def _safe_float(self, v: Any) -> Optional[float]:
        if v is None:
            return None
        try:
            return float(v)
        except Exception:
            return None
