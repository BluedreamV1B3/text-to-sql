import pyodbc
from typing import Any, Dict, List, Optional


class SqlServerExecutor:
    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.conn: Optional[pyodbc.Connection] = None

    def connect(self) -> None:
        if self.conn is None:
            self.conn = pyodbc.connect(self.connection_string)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _session_times(self) -> Dict[str, int]:
        """
        Returns server-side counters for *this connection's session*.
        cpu_time: ms CPU used by session
        total_elapsed_time: ms elapsed since session started
        """
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT cpu_time, total_elapsed_time
            FROM sys.dm_exec_sessions
            WHERE session_id = @@SPID
            """
        )
        row = cur.fetchone()
        cur.close()
        return {"cpu_time": int(row[0]), "total_elapsed_time": int(row[1])}

    def execute(self, sql: str, params: Optional[List[Any]] = None) -> Dict[str, Any]:
        if self.conn is None:
            raise RuntimeError("SqlServerExecutor not connected. Call connect().")

        params = params or []

        # Server counters BEFORE
        t0 = self._session_times()

        cur = self.conn.cursor()
        cur.execute(sql, params)

        # Fetch all rows (for SELECT). If non-SELECT, this will throw; handle gracefully.
        rows = []
        columns = []
        row_count = None

        try:
            if cur.description:
                columns = [c[0] for c in cur.description]
                fetched = cur.fetchall()
                rows = [dict(zip(columns, r)) for r in fetched]
                row_count = len(rows)
            else:
                # For INSERT/UPDATE/DELETE
                row_count = cur.rowcount
        except pyodbc.ProgrammingError:
            # No results to fetch
            row_count = cur.rowcount

        cur.close()

        # Server counters AFTER
        t1 = self._session_times()

        return {
            "rows": rows,
            "row_count": row_count,
            "server_cpu_ms": max(0, t1["cpu_time"] - t0["cpu_time"]),
            "server_elapsed_ms": max(0, t1["total_elapsed_time"] - t0["total_elapsed_time"]),
        }

import db_executor
