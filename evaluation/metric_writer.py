# metric_writer.py
import json
import threading
import time
import random
from typing import Any, Dict, List, Optional, Tuple

import pyodbc

from metrics_events import WriteEvent


class MetricWriter:
    """
    Drains WriteEvent queue and writes:
      - FACT events to SQL Server (batched) and/or facts.jsonl
      - RUN_HEADER to SQL Server (query_runs)
      - RUN_METRICS to SQL Server and/or run_metrics.jsonl (legacy / optional)
      - METRIC events to metrics.jsonl

    Features:
      - Writer stats (events processed, last flush time, buffer sizes, counts by type, last error)
      - DB retry/backoff for transient errors
      - Keeps JSONL files open and flushes periodically
    """

    TRANSIENT_SQLSTATES = {
        "08S01",  # Communication link failure
        "08001",  # Client unable to establish connection
        "HYT00",  # Timeout expired
        "HYT01",  # Connection timeout
        "40001",  # Serialization failure
    }

    TRANSIENT_NATIVE_ERRORS = {
        1205,  # Deadlock victim
        4060,
        40197, 40501, 40613,
        10928, 10929,
        10053, 10054, 10060,
    }

    def __init__(
        self,
        event_queue,
        connection_string: Optional[str] = None,
        facts_table: str = "dbo.query_execution_facts",
        runs_table: str = "dbo.query_runs",
        run_metrics_table: str = "dbo.query_run_metrics",
        facts_jsonl_path: Optional[str] = None,
        run_metrics_jsonl_path: Optional[str] = None,
        metrics_jsonl_path: Optional[str] = None,
        facts_batch_size: int = 200,
        flush_every_n_events: int = 200,
        flush_every_seconds: float = 2.0,
        # Retry/backoff
        db_max_retries: int = 5,
        db_backoff_base_seconds: float = 0.25,
        db_backoff_max_seconds: float = 8.0,
        db_backoff_jitter: float = 0.2,  # +/- 20%
    ):
        self.q = event_queue
        self.connection_string = connection_string

        self.facts_table = facts_table
        self.runs_table = runs_table
        self.run_metrics_table = run_metrics_table

        self.facts_jsonl_path = facts_jsonl_path
        self.run_metrics_jsonl_path = run_metrics_jsonl_path
        self.metrics_jsonl_path = metrics_jsonl_path

        self.facts_batch_size = max(1, int(facts_batch_size))
        self.flush_every_n_events = max(1, int(flush_every_n_events))
        self.flush_every_seconds = max(0.1, float(flush_every_seconds))

        self.db_max_retries = max(0, int(db_max_retries))
        self.db_backoff_base_seconds = max(0.01, float(db_backoff_base_seconds))
        self.db_backoff_max_seconds = max(self.db_backoff_base_seconds, float(db_backoff_max_seconds))
        self.db_backoff_jitter = max(0.0, float(db_backoff_jitter))

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # buffers
        self._facts_buf: List[Dict[str, Any]] = []
        self._events_since_flush = 0
        self._last_flush_monotonic = time.monotonic()

        # open file handles
        self._facts_fh = None
        self._run_metrics_fh = None
        self._metrics_fh = None

        # stats
        self._stats_lock = threading.Lock()
        self._events_processed = 0
        self._events_by_type: Dict[str, int] = {}
        self._db_retries_total = 0
        self._db_failures_total = 0
        self._last_error: Optional[str] = None
        self._last_db_error: Optional[str] = None

    # ---------------- public API ----------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="metric-writer", daemon=True)
        self._thread.start()

    def stop(self, drain: bool = True) -> None:
        """
        Enqueue STOP. If drain=True, writer will process everything already queued before stopping.
        """
        if self._thread is None:
            return
        self.q.put(WriteEvent(event_type="STOP", run_id="*", payload={"drain": drain}))
        self._thread.join()
        self._thread = None

    def get_stats(self) -> Dict[str, Any]:
        with self._stats_lock:
            now_mon = time.monotonic()
            seconds_since_flush = max(0.0, now_mon - self._last_flush_monotonic)
            qsize = getattr(self.q, "qsize", lambda: None)()
            return {
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "events_processed": self._events_processed,
                "events_by_type": dict(self._events_by_type),
                "queue_size_approx": qsize,
                "facts_buffer_len": len(self._facts_buf),
                "events_since_flush": self._events_since_flush,
                "seconds_since_last_flush": seconds_since_flush,
                "db_retries_total": self._db_retries_total,
                "db_failures_total": self._db_failures_total,
                "last_error": self._last_error,
                "last_db_error": self._last_db_error,
            }

    # ---------------- internal loop ----------------

    def _run(self) -> None:
        conn = None
        cur = None

        try:
            # ---- DB connect (optional)
            if self.connection_string:
                try:
                    conn = pyodbc.connect(self.connection_string)
                    cur = conn.cursor()
                    cur.fast_executemany = True
                except Exception as e:
                    self._set_last_error(f"DB connect failed: {e}")
                    conn = None
                    cur = None

            # ---- open JSONL handles (optional)
            if self.facts_jsonl_path:
                self._facts_fh = open(self.facts_jsonl_path, "a", encoding="utf-8", buffering=1)  # line buffered
            if self.run_metrics_jsonl_path:
                self._run_metrics_fh = open(self.run_metrics_jsonl_path, "a", encoding="utf-8", buffering=1)
            if self.metrics_jsonl_path:
                self._metrics_fh = open(self.metrics_jsonl_path, "a", encoding="utf-8", buffering=1)

            # ---- main loop
            while not self._stop.is_set():
                ev: WriteEvent = self.q.get()
                self._count_event(ev.event_type)

                if ev.event_type == "STOP":
                    drain = bool((ev.payload or {}).get("drain", True))
                    if drain:
                        self._drain_remaining(cur, conn)
                    self._flush_all(cur, conn, force=True)
                    break

                if ev.event_type == "FACT":
                    # DB batch buffer
                    if isinstance(ev.payload, dict):
                        self._facts_buf.append(ev.payload)
                    # JSONL
                    self._write_fact_jsonl(ev)

                    if len(self._facts_buf) >= self.facts_batch_size:
                        self._flush_facts_with_retry(cur, conn)

                elif ev.event_type == "RUN_HEADER":
                    if isinstance(ev.payload, dict):
                        self._run_db_with_retry(cur, conn, self._write_run_header, ev.payload)

                elif ev.event_type == "RUN_METRICS":
                    if isinstance(ev.payload, dict):
                        self._run_db_with_retry(cur, conn, self._write_run_metrics, ev.payload)
                        self._write_run_metrics_jsonl(ev.payload)

                elif ev.event_type == "METRIC":
                    self._write_metric_jsonl(ev)

                elif ev.event_type == "FLUSH":
                    self._flush_all(cur, conn, force=True)

                # periodic flush checks
                self._events_since_flush += 1
                now = time.monotonic()
                if (
                    self._events_since_flush >= self.flush_every_n_events
                    or (now - self._last_flush_monotonic) >= self.flush_every_seconds
                ):
                    self._flush_all(cur, conn, force=False)

        finally:
            # best-effort final flush & close
            try:
                self._flush_all(cur, conn, force=True)
            except Exception as e:
                self._set_last_error(f"final flush failed: {e}")

            # close db
            if conn:
                try:
                    conn.commit()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass

            # close files (IMPORTANT: includes metrics_fh)
            self._close_files()

    def _drain_remaining(self, cur, conn) -> None:
        """
        Drain any events already queued without blocking.
        """
        while True:
            try:
                ev: WriteEvent = self.q.get_nowait()
            except Exception:
                break

            self._count_event(ev.event_type)

            if ev.event_type == "FACT":
                if isinstance(ev.payload, dict):
                    self._facts_buf.append(ev.payload)
                self._write_fact_jsonl(ev)
                if len(self._facts_buf) >= self.facts_batch_size:
                    self._flush_facts_with_retry(cur, conn)

            elif ev.event_type == "RUN_HEADER":
                if isinstance(ev.payload, dict):
                    self._run_db_with_retry(cur, conn, self._write_run_header, ev.payload)

            elif ev.event_type == "RUN_METRICS":
                if isinstance(ev.payload, dict):
                    self._run_db_with_retry(cur, conn, self._write_run_metrics, ev.payload)
                    self._write_run_metrics_jsonl(ev.payload)

            elif ev.event_type == "METRIC":
                self._write_metric_jsonl(ev)

            elif ev.event_type == "FLUSH":
                self._flush_all(cur, conn, force=True)

            elif ev.event_type == "STOP":
                # ignore nested stop
                continue

    # ---------------- flushing ----------------

    def _flush_all(self, cur, conn, force: bool) -> None:
        self._flush_facts_with_retry(cur, conn)

        # Flush files (IMPORTANT: includes metrics_fh)
        if self._facts_fh:
            self._facts_fh.flush()
        if self._run_metrics_fh:
            self._run_metrics_fh.flush()
        if self._metrics_fh:
            self._metrics_fh.flush()

        with self._stats_lock:
            self._events_since_flush = 0
            self._last_flush_monotonic = time.monotonic()

    def _flush_facts_with_retry(self, cur, conn) -> None:
        if not self._facts_buf:
            return

        # If DB not configured, just clear buffer (JSONL may still capture facts)
        if not (cur and conn):
            self._facts_buf.clear()
            return

        rows = [self._fact_to_tuple(f) for f in self._facts_buf]

        def op():
            cur.executemany(self._facts_insert_sql(), rows)
            conn.commit()

        self._retry_db(op, context="flush_facts")
        self._facts_buf.clear()

    # ---------------- DB retry/backoff ----------------

    def _run_db_with_retry(self, cur, conn, fn, payload: Dict[str, Any]) -> None:
        if not (cur and conn):
            return

        def op():
            fn(cur, payload)
            conn.commit()

        self._retry_db(op, context=fn.__name__)

    def _retry_db(self, op, context: str) -> None:
        attempt = 0
        while True:
            try:
                op()
                return
            except pyodbc.Error as e:
                attempt += 1
                is_transient, reason = self._is_transient_db_error(e)
                self._set_last_db_error(f"{context}: {e} (transient={is_transient}; {reason})")

                if (not is_transient) or attempt > self.db_max_retries:
                    with self._stats_lock:
                        self._db_failures_total += 1
                    self._set_last_error(f"DB write failed permanently in {context}: {e}")
                    return

                with self._stats_lock:
                    self._db_retries_total += 1

                time.sleep(self._backoff_seconds(attempt))

            except Exception as e:
                self._set_last_error(f"{context}: non-db exception: {e}")
                return

    def _is_transient_db_error(self, e: pyodbc.Error) -> Tuple[bool, str]:
        sqlstate = None
        msg = ""

        try:
            if len(e.args) >= 1 and isinstance(e.args[0], str):
                sqlstate = e.args[0]
            if len(e.args) >= 2 and isinstance(e.args[1], str):
                msg = e.args[1]
        except Exception:
            pass

        native = None
        if msg:
            for n in self.TRANSIENT_NATIVE_ERRORS:
                if f"({n})" in msg or f"[{n}]" in msg:
                    native = n
                    break

        if sqlstate in self.TRANSIENT_SQLSTATES:
            return True, f"sqlstate={sqlstate}"
        if native in self.TRANSIENT_NATIVE_ERRORS:
            return True, f"native={native}"

        lower = (msg or "").lower()
        if "deadlock" in lower:
            return True, "deadlock"
        if "timeout" in lower:
            return True, "timeout"
        if "could not open a connection" in lower or "communication link failure" in lower:
            return True, "connection"

        return False, f"sqlstate={sqlstate or 'unknown'}"

    def _backoff_seconds(self, attempt: int) -> float:
        base = self.db_backoff_base_seconds * (2 ** (attempt - 1))
        base = min(base, self.db_backoff_max_seconds)
        if self.db_backoff_jitter > 0:
            jitter = 1.0 + random.uniform(-self.db_backoff_jitter, self.db_backoff_jitter)
            base *= jitter
        return max(0.01, base)

    # ---------------- DB SQL ----------------

    def _facts_insert_sql(self) -> str:
        return f"""
        INSERT INTO {self.facts_table} (
            run_id, job_id,
            ok, error,
            server_cpu_ms, server_elapsed_ms, row_count,
            sql_text, params_json, metadata_json, rows_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

    def _fact_to_tuple(self, f: Dict[str, Any]) -> tuple:
        return (
            f.get("run_id"),
            f.get("job_id"),
            1 if f.get("ok") else 0,
            f.get("error"),
            f.get("server_cpu_ms"),
            f.get("server_elapsed_ms"),
            f.get("row_count"),
            f.get("sql_text"),
            f.get("params_json"),
            f.get("metadata_json"),
            f.get("rows_json"),
        )

    def _write_run_header(self, cur, payload: Dict[str, Any]) -> None:
        cur.execute(
            f"INSERT INTO {self.runs_table} (run_id, created_at_utc, label, notes) VALUES (?, ?, ?, ?)",
            payload["run_id"], payload["created_at_utc"], payload.get("label"), payload.get("notes"),
        )

    def _write_run_metrics(self, cur, payload: Dict[str, Any]) -> None:
        rows = payload["rows"]
        cur.executemany(
            f"""
            INSERT INTO {self.run_metrics_table}
            (run_id, metric_name, metric_value, metric_text, created_at_utc)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )

    # ---------------- JSONL writes ----------------

    def _write_metric_jsonl(self, ev: WriteEvent) -> None:
        if not self._metrics_fh:
            return
        record = {
            "event_type": ev.event_type,
            "run_id": ev.run_id,
            "job_id": ev.job_id,
            "sql": ev.sql,
            "metadata": ev.metadata,
            **(ev.payload or {}),
        }
        self._metrics_fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _write_fact_jsonl(self, ev: WriteEvent) -> None:
        if not self._facts_fh:
            return
        record = {
            "event_type": ev.event_type,
            "run_id": ev.run_id,
            "job_id": ev.job_id,
            "sql": ev.sql,
            "metadata": ev.metadata,
            **(ev.payload or {}),
        }
        self._facts_fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _write_run_metrics_jsonl(self, payload: Dict[str, Any]) -> None:
        if not self._run_metrics_fh:
            return
        self._run_metrics_fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    # ---------------- stats helpers ----------------

    def _count_event(self, event_type: str) -> None:
        with self._stats_lock:
            self._events_processed += 1
            self._events_by_type[event_type] = self._events_by_type.get(event_type, 0) + 1

    def _set_last_error(self, msg: str) -> None:
        with self._stats_lock:
            self._last_error = msg

    def _set_last_db_error(self, msg: str) -> None:
        with self._stats_lock:
            self._last_db_error = msg

    def _close_files(self) -> None:
        for attr in ("_facts_fh", "_run_metrics_fh", "_metrics_fh"):
            fh = getattr(self, attr, None)
            if fh:
                try:
                    fh.flush()
                except Exception:
                    pass
                try:
                    fh.close()
                except Exception:
                    pass
                setattr(self, attr, None)
