# metrics_events.py
from dataclasses import dataclass
from typing import Any, Dict, Optional, Literal

EventType = Literal["FACT", "METRIC", "RUN_HEADER", "FLUSH", "STOP"]


@dataclass(frozen=True)
class WriteEvent:
    event_type: EventType
    run_id: str

    # Observability fields (make indexing + golden writing easy)
    job_id: Optional[str] = None
    sql: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    # Event body
    payload: Optional[Dict[str, Any]] = None