"""Fault target: hold a real PostgreSQL session while the execution thread stalls."""

import json
import os
import sys
import threading

from app.p0.persistence import locked_saver
from app.p0.store import Store


def main():
    spec = json.loads(sys.stdin.readline())
    store = Store(os.environ["P0_MYSQL_URL"])
    run = store.read_run(spec["run_id"])
    with locked_saver(
        os.environ["P0_POSTGRES_URL"], run["session_id"], instance=spec["instance"]
    ) as (_, connection):
        assert store.claim(run["id"], lease_seconds=spec["lease_seconds"])
        identity = connection.execute(
            "SELECT pid, backend_start, application_name FROM pg_stat_activity "
            "WHERE pid=pg_backend_pid()"
        ).fetchone()
        identity["backend_start"] = identity["backend_start"].isoformat()
        identity["lease_until"] = store.read_run(run["id"])["lease_until"]
        print(json.dumps(identity), flush=True)
        if spec["mode"] == "exit":
            os._exit(74)
        threading.Event().wait()  # SDK/event-loop deadlock; PG connection remains open.


if __name__ == "__main__":
    main()
