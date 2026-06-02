# metric_manager.py
import json
import uuid
import datetime as dt
from typing import Any, Dict, List, Optional, Callable

from metrics_events import WriteEvent


def utcnow_naive() -> dt.datetime:
    return dt.datetime.utcnow().replace(tzinfo=None)


class MetricManager:
    """
    Emits:
      - FACT event (optional)
      - METRIC event(s): one per metric key/value for each QueryResult
    No run-level metrics.
    """

    def __init__(
        self,
        writer_queue,
        run_id: Optional[uuid.UUID] = None,
        run_label: Optional[str] = None,
        *,
        enable_fact_writes: bool = True,
        enable_metric_writes: bool = True,
        include_rows: bool = True,
        rows_max_chars: int = 200_000,
    ):
        self.qw = writer_queue
        self.run_id = run_id or uuid.uuid4()
        self.run_label = run_label

        self.enable_fact_writes = enable_fact_writes
        self.enable_metric_writes = enable_metric_writes

        self.include_rows = include_rows
        self.rows_max_chars = rows_max_chars

        self._per_query_metric_fns: List[Callable[[Any], Dict[str, Any]]] = []

    def enqueue_run_header(self, notes: Optional[str] = None) -> None:
        self.qw.put(
            WriteEvent(
                event_type="RUN_HEADER",
                run_id=str(self.run_id),
                payload={
                    "run_id": str(self.run_id),
                    "created_at_utc": utcnow_naive(),
                    "label": self.run_label,
                    "notes": notes,
                },
            )
        )

    def flush_writes(self) -> None:
        self.qw.put(WriteEvent(event_type="FLUSH", run_id=str(self.run_id), payload={}))

    # -------- metric registration --------

    def register_per_query_metric(self, fn: Callable[[Any], Dict[str, Any]]) -> None:
        self._per_query_metric_fns.append(fn)

    def _compute_per_query_metrics(self, result: Any) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for fn in self._per_query_metric_fns:
            try:
                extra = fn(result)
                if isinstance(extra, dict) and extra:
                    out.update(extra)
            except Exception as e:
                # Emit an error metric instead of killing metric emission
                name = getattr(fn, "__name__", "metric")
                out[f"metric_error::{name}"] = f"{type(e).__name__}: {e}"
        return out


    # -------- collection entrypoint --------

    def add_result(self, result: Any) -> None:
        job = getattr(result, "job", None)
        job_id = getattr(job, "job_id", None) if job else getattr(result, "job_id", None)
        job_id_str = str(job_id) if job_id is not None else None

        sql = getattr(job, "sql", None) if job else getattr(result, "sql", None)
        metadata = getattr(job, "metadata", None) if job else getattr(result, "metadata", None)

        # 1) FACT event (optional)
        if self.enable_fact_writes:
            fact = self._result_to_fact(result)
            self.qw.put(
                WriteEvent(
                    event_type="FACT",
                    run_id=str(self.run_id),
                    job_id=job_id_str,
                    sql=sql,
                    metadata=metadata,
                    payload=fact,
                )
            )

        # 2) METRIC events (one per metric)
        if self.enable_metric_writes:
            metrics = self._compute_per_query_metrics(result)
            created_at = utcnow_naive()
            for name, value in metrics.items():
                self.qw.put(
                    WriteEvent(
                        event_type="METRIC",
                        run_id=str(self.run_id),
                        job_id=job_id_str,
                        sql=sql,
                        metadata=metadata,
                        payload={
                            "run_id": str(self.run_id),
                            "run_label": self.run_label,
                            "job_id": job_id_str,
                            "metric_name": name,
                            "metric_value": value,   # writer will split numeric/text if needed
                            "created_at_utc": created_at,
                        },
                    )
                )

    # -------- fact helpers --------

    def _safe_json(self, obj: Any) -> Optional[str]:
        if obj is None:
            return None
        return json.dumps(obj, ensure_ascii=False, default=str)

    def _maybe_rows_json(self, rows: Any) -> Optional[str]:
        if not self.include_rows:
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
    
        return {
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
    
            # FULL result set for golden creation
            "rows": rows,
    
            # optional compact/truncated JSON version
            "rows_json": self._maybe_rows_json(rows),
        }        

