# golden_set_writer.py
import csv
import json
import os
import queue
import threading
import time
from typing import Any, Dict, Optional

from metrics_events import WriteEvent


class GoldenSetWriter:
    """
    Consumes WriteEvent objects and writes a Golden Set for each FACT event.

    Output:
      1) One JSON per request_id containing:
         - request_id, sql, metadata, observed server times, row_count
         - FULL result rows (payload["rows"])
      2) One CSV index (append-only) for easy editing of targets and overview.

    Notes:
      - request_id is pulled from ev.metadata[request_id_key]
      - If missing, falls back to ev.job_id (not ideal, but safe)
      - Keeps CSV file handle open; flushes periodically
    """

    def __init__(
        self,
        event_queue,
        *,
        out_dir: str,
        index_csv_path: str,
        request_id_key: str = "request_id",
        overwrite_json: bool = True,
        flush_every_n: int = 100,
        flush_every_seconds: float = 2.0,
    ):
        self.q = event_queue
        self.out_dir = out_dir
        self.index_csv_path = index_csv_path
        self.request_id_key = request_id_key
        self.overwrite_json = overwrite_json

        self.flush_every_n = max(1, int(flush_every_n))
        self.flush_every_seconds = max(0.1, float(flush_every_seconds))

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._index_fh = None
        self._csv_writer: Optional[csv.DictWriter] = None

        self._events_processed = 0
        self._last_flush_monotonic = time.monotonic()
        self._last_error: Optional[str] = None

        os.makedirs(self.out_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.index_csv_path) or ".", exist_ok=True)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="golden-writer", daemon=True)
        self._thread.start()

    def stop(self, drain: bool = True) -> None:
        if self._thread is None:
            return
        self.q.put(WriteEvent(event_type="STOP", run_id="*", payload={"drain": drain}))
        self._thread.join()
        self._thread = None

    def get_stats(self) -> Dict[str, Any]:
        return {
            "events_processed": self._events_processed,
            "last_flush_time": self._last_flush_monotonic,
            "last_error": self._last_error,
        }

    # -------------------------
    # Main loop
    # -------------------------

    def _run(self) -> None:
        self._index_fh = open(self.index_csv_path, "a", newline="", encoding="utf-8")
        try:
            last_flush = time.monotonic()

            while not self._stop.is_set():
                try:
                    ev: WriteEvent = self.q.get(timeout=0.2)
                except queue.Empty:
                    ev = None

                now = time.monotonic()
                if (now - last_flush) >= self.flush_every_seconds:
                    self._flush()
                    last_flush = now

                if ev is None:
                    continue

                if ev.event_type == "STOP":
                    drain = bool((ev.payload or {}).get("drain", True))
                    if drain:
                        self._drain_remaining()
                    self._flush()
                    break

                if ev.event_type == "FLUSH":
                    self._flush()
                    last_flush = time.monotonic()
                    continue

                if ev.event_type != "FACT":
                    continue

                self._handle_fact(ev)

                self._events_processed += 1
                if (self._events_processed % self.flush_every_n) == 0:
                    self._flush()
                    last_flush = time.monotonic()

        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            raise
        finally:
            try:
                self._flush()
            except Exception:
                pass
            try:
                self._index_fh.close()
            except Exception:
                pass
            self._index_fh = None

    def _drain_remaining(self) -> None:
        while True:
            try:
                ev: WriteEvent = self.q.get_nowait()
            except queue.Empty:
                break
            if ev.event_type == "FACT":
                self._handle_fact(ev)

    def _flush(self) -> None:
        if self._index_fh:
            self._index_fh.flush()
        self._last_flush_monotonic = time.monotonic()

    # -------------------------
    # FACT handling
    # -------------------------

    def _handle_fact(self, ev: WriteEvent) -> None:
        md = ev.metadata if isinstance(ev.metadata, dict) else {}
        payload = ev.payload or {}
        question = md.get("question")

        request_id = md.get(self.request_id_key) or payload.get(self.request_id_key) or ev.job_id or "unknown"

        # Full rows preferred (your MetricManager now includes payload["rows"])
        rows = payload.get("rows", None)

        golden_path = os.path.join(self.out_dir, f"{request_id}.json")
        if (not self.overwrite_json) and os.path.exists(golden_path):
            # Still write index row (optional), but skip JSON overwrite
            self._write_index_row(request_id, golden_path, ev, payload, md)
            return

        golden_record = {
            "request_id": request_id,
            "question": question,
            "run_id": payload.get("run_id") or ev.run_id,
            "run_label": payload.get("run_label"),
            "job_id": payload.get("job_id") or ev.job_id,

            # "golden control plane"
            "sql": ev.sql or payload.get("sql_text"),
            "params_json": payload.get("params_json"),
            "metadata": md,

            # Observed execution facts
            "observed": {
                "ok": payload.get("ok"),
                "error": payload.get("error"),
                "row_count": payload.get("row_count"),
                "server_cpu_ms": payload.get("server_cpu_ms"),
                "server_elapsed_ms": payload.get("server_elapsed_ms"),
            },

            # Full result set
            "result": {
                "rows": rows,
            },
        }

        with open(golden_path, "w", encoding="utf-8") as f:
            json.dump(golden_record, f, ensure_ascii=False, default=str, indent=2)

        self._write_index_row(request_id, golden_path, ev, payload, md)

    def _write_index_row(
        self,
        request_id: str,
        golden_path: str,
        ev: WriteEvent,
        payload: Dict[str, Any],
        md: Dict[str, Any],
    ) -> None:
        """
        Append an index row. This is intentionally simple (append-only).
        You can edit targets later in the CSV.
        """
        row = {
            "request_id": request_id,

            # editable targets (you supply these in submit metadata)
            "target_server_elapsed_ms": md.get("target_server_elapsed_ms"),
            "target_server_cpu_ms": md.get("target_server_cpu_ms"),

            # observed
            "observed_server_elapsed_ms": payload.get("server_elapsed_ms"),
            "observed_server_cpu_ms": payload.get("server_cpu_ms"),
            "row_count": payload.get("row_count"),
            "ok": payload.get("ok"),

            # tracing
            "run_id": payload.get("run_id") or ev.run_id,
            "job_id": payload.get("job_id") or ev.job_id,

            "sql": (ev.sql or payload.get("sql_text") or ""),
            "golden_json_path": golden_path,
        }

        if self._csv_writer is None:
            fieldnames = list(row.keys())
            self._csv_writer = csv.DictWriter(self._index_fh, fieldnames=fieldnames)
            if self._index_fh.tell() == 0:
                self._csv_writer.writeheader()

        self._csv_writer.writerow(row)
