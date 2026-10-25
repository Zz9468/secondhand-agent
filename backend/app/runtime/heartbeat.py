"""A bounded lease keeper, independent of the synchronous model SDK call."""

from contextlib import contextmanager
from threading import Event, Thread


@contextmanager
def keep_lease(queue, run_id, owner):
    stop = Event()

    def tick():
        while not stop.wait(5):
            try:
                queue.heartbeat(run_id, owner)
            except Exception:
                # Business writes still verify ownership; never revive an expired lease.
                return

    worker = Thread(target=tick, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=3)
