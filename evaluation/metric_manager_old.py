# metric_manager.py
import json
import uuid

from typing import Any, Dict, Optional, Literal
from metrics_events import WriteEvent


class MetricManager:
    def __init__(
        self,
        writer_queue,
        run_id: Optional[uuid.UUID] = None,
        run_label: Optional[str] = None,
        *,
        include_rows_full: bool = True,   # NEW: store full rows in payload
        include_rows_json: bool = True,   # keep optional truncated json too
        rows_max_chars: int = 200_000,
        enable_fact_writes: bool = True,
        enable_metric_writes: bool = True,
    ):
        self.qw = writer_queue
        self.run_id = run_id or uuid.uuid4()
        self.run_label = run_label

        self.include_rows_full = include_rows_full
        self.include_rows_json = include_rows_json
        self.rows_max_chars = rows_max_chars
        self.enable_fact_writes = enable_fact_writes
        self.enable_metric_writes = enable_metric_writes

    def _safe_json(self, obj: Any) -> Optional[str]:
        if obj is None:
            return None
        return json.dumps(obj, ensure_ascii=False, default=str)

    def _maybe_rows_json(self, rows: Any) -> Optional[str]:
        if not self.include_rows_json:
            return None
        s = self._safe_json(rows)
        if s is None:
            return None
        if len(s) > self.rows_max_chars:
            return s[: self.rows_max_chars] + "...(truncated)"
        return s

    def _result_to_fact(self, r: Any) -> Dict[str, Any]:
        job = getattr(r, "job", None)

        job_id = getattr(job, "job_id", None) if job else getattr(r, "job_id", None)
        sql_text = getattr(job, "sql", None) if job else getattr(r, "sql", None)
        params = getattr(job, "params", None) if job else getattr(r, "params", None)
        metadata = getattr(job, "metadata", None) if job else getattr(r, "metadata", None)

        error = getattr(r, "error", None)
        if isinstance(error, str) and len(error) > 4000:
            error = error[:4000]

        rows = getattr(r, "rows", None)

        fact = {
            "run_id": str(self.run_id),
            "run_label": self.run_label,
            "job_id": str(job_id) if job_id is not None else None,
            "ok": bool(getattr(r, "ok", False)),
            "error": error,
            "server_cpu_ms": getattr(r, "server_cpu_ms", None),
            "server_elapsed_ms": getattr(r, "server_elapsed_ms", None),
            "row_count": getattr(r, "row_count", None),
            "sql_text": sql_text,
            "params_json": self._safe_json(params),
            "metadata_json": self._safe_json(metadata),
        }

        # NEW: full, untruncated rows (needed for golden set)
        if self.include_rows_full:
            fact["rows"] = rows

        # Optional: keep a truncated JSON string version too
        fact["rows_json"] = self._maybe_rows_json(rows)

        return fact

    def add_result(self, result: Any) -> None:
        """
        Called from ParallelQueryRunner.result_callback.
        Emits FACT events with sql + metadata at the event level, and rows in payload.
        """
        if not self.enable_fact_writes:
            return

        job = getattr(result, "job", None)
        job_id = getattr(job, "job_id", None) if job else getattr(result, "job_id", None)
        sql = getattr(job, "sql", None) if job else getattr(result, "sql", None)
        metadata = getattr(job, "metadata", None) if job else getattr(result, "metadata", None)

        # Ensure metadata is a dict if present
        md = metadata if isinstance(metadata, dict) else None

        fact = self._result_to_fact(result)

        self.qw.put(
            WriteEvent(
                event_type="FACT",
                run_id=str(self.run_id),
                job_id=str(job_id) if job_id is not None else None,
                sql=sql,
                metadata=md,
                payload=fact,
            )
        )

    def flush_writes(self) -> None:
        # Ask the writer to flush any buffered output
        self.qw.put(WriteEvent(event_type="FLUSH", run_id=str(self.run_id), payload={}))
