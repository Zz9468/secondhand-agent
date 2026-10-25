"""One PostgreSQL connection owns both the advisory lock and official saver."""

from contextlib import contextmanager
from hashlib import sha256

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row


class ThreadBusy(RuntimeError):
    pass


def lock_key(thread_id: str) -> int:
    raw = sha256(f"secondhand-agent-langgraph:{thread_id}".encode()).digest()[:8]
    return int.from_bytes(raw, "big", signed=True)


@contextmanager
def locked_saver(url: str, thread_id: str, *, instance: str = "p0", on_connect=None):
    # No pool, reconnect or setup here. A failed connection ends this runner.
    with psycopg.connect(
        url,
        autocommit=True,
        row_factory=dict_row,
        prepare_threshold=0,
        connect_timeout=3,
        application_name=f"secondhand-p0:{instance}",
        options="-c statement_timeout=5000 -c lock_timeout=2000",
    ) as connection:
        if on_connect:
            on_connect(connection)
        key = lock_key(thread_id)
        acquired = connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (key,)
        ).fetchone()["acquired"]
        if not acquired:
            raise ThreadBusy(thread_id)
        try:
            yield PostgresSaver(connection), connection
        finally:
            if not connection.closed and not connection.broken:
                connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
