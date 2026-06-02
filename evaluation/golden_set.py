import os
import json
from typing import Any, Dict, List, Optional


class GoldenSet:
    def __init__(self, out_dir: str, *, request_id_key: str = "request_id"):
        self.out_dir = out_dir
        self.request_id_key = request_id_key

    # --- core file lookup ---

    def golden_path(self, request_id: str) -> str:
        """
        Return the expected golden JSON path for a request_id.
        """
        return os.path.join(self.out_dir, f"{request_id}.json")

    def load_record(self, request_id: str) -> Dict[str, Any]:
        """
        Load the full golden record from disk for a request_id.
        Raises FileNotFoundError if missing.
        """
        path = self.golden_path(request_id)
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        # normalize
        rec["request_id"] = str(rec.get("request_id") or request_id)
        rec["_path"] = path
        return rec

    def load_rows(self, request_id: str) -> List[Dict[str, Any]]:
        """
        Load ONLY the golden result set (rows) for a request_id.
        This is what you'll use for accuracy comparisons.
        """
        rec = self.load_record(request_id)
        rows = (rec.get("result") or {}).get("rows")
        return rows if isinstance(rows, list) else []

    # --- convenience: resolve from a QueryResult ---

    def load_rows_for_result(self, result: Any) -> List[Dict[str, Any]]:
        """
        Resolve request_id from QueryResult.job.metadata and load golden rows.
        """
        job = getattr(result, "job", None)
        md = getattr(job, "metadata", None)
        if not isinstance(md, dict):
            raise KeyError("QueryResult.job.metadata is missing or not a dict")

        request_id = md.get(self.request_id_key)
        if not request_id:
            raise KeyError(f"metadata['{self.request_id_key}'] is missing")

        return self.load_rows(str(request_id))
