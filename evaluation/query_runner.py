import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable

from db_executor import SqlServerExecutor


@dataclass(frozen=True)
class QueryJob:
    job_id: str
    sql: str
    params: List[Any] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QueryResult:
    job: QueryJob
    ok: bool
    rows: Optional[List[Dict[str, Any]]] = None
    row_count: Optional[int] = None
    server_cpu_ms: Optional[int] = None
    server_elapsed_ms: Optional[int] = None
    error: Optional[str] = None


class ParallelQueryRunner:
    def __init__(
        self,
        connection_string: str,
        num_workers: int = 4,
        max_queue_size: int = 0,
        result_callback: Optional[Callable[[QueryResult], None]] = None,
        executor_cls=SqlServerExecutor,
    ):
        self.connection_string = connection_string
        self.num_workers = num_workers
        self.result_callback = result_callback
        self.executor_cls = executor_cls

        self._job_queue: "queue.Queue[Optional[QueryJob]]" = queue.Queue(maxsize=max_queue_size)
        self._result_queue: "queue.Queue[QueryResult]" = queue.Queue()

        self._threads: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._started = False

    def result_queue(self) -> "queue.Queue[QueryResult]":
        return self._result_queue

    def start(self) -> None:
        if self._started:
            return
        self._stop_event.clear()
        self._threads = []
        for i in range(self.num_workers):
            t = threading.Thread(target=self._worker_loop, name=f"sql-worker-{i+1}", daemon=True)
            t.start()
            self._threads.append(t)
        self._started = True

    def stop(self, drain: bool = True) -> None:
        if not self._started:
            return
        if not drain:
            self._stop_event.set()
        for _ in range(self.num_workers):
            self._job_queue.put(None)
        for t in self._threads:
            t.join()
        self._started = False

    def submit(self, sql: str, params: Optional[List[Any]] = None, metadata: Optional[Dict[str, Any]] = None) -> str:
        if not self._started:
            raise RuntimeError("ParallelQueryRunner not started. Call start().")
        job_id = str(uuid.uuid4())
        job = QueryJob(job_id=job_id, sql=sql, params=params or [], metadata=metadata or {})
        self._job_queue.put(job)
        return job_id

    def get_result(self, timeout: Optional[float] = None) -> QueryResult:
        return self._result_queue.get(timeout=timeout)

    def _worker_loop(self) -> None:
        executor = self.executor_cls(self.connection_string)
        executor.connect()
        try:
            while not self._stop_event.is_set():
                job = self._job_queue.get()
                if job is None:
                    break
                try:
                    payload = executor.execute(job.sql, job.params)
                    result = QueryResult(
                        job=job,
                        ok=True,
                        rows=payload["rows"],
                        row_count=payload["row_count"],
                        server_cpu_ms=payload["server_cpu_ms"],
                        server_elapsed_ms=payload["server_elapsed_ms"],
                    )
                except Exception as e:
                    result = QueryResult(job=job, ok=False, error=str(e))

                self._result_queue.put(result)

                if self.result_callback:
                    try:
                        self.result_callback(result)
                    except Exception:
                        pass
        finally:
            executor.close()