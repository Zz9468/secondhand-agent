"""Independent P0 supervisor: revoke ownership, terminate child, verify PG release."""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo

from app.p0.persistence import locked_saver
from app.p0.store import Store


def main():
    spec = json.loads(sys.stdin.read())
    spec["instance"] = uuid4().hex
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("P0_POSTGRES_ADMIN_URL", "P0_POSTGRES_CLEANER_URL")
    }
    if spec.get("proxy_port"):
        env["P0_POSTGRES_URL"] = make_conninfo(
            env["P0_POSTGRES_URL"], port=spec["proxy_port"], sslmode="disable"
        )
    child = subprocess.Popen(
        [sys.executable, "-m", "scripts.p0_hung_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    store = Store(os.environ["P0_MYSQL_URL"])
    try:
        child.stdin.write(json.dumps(spec) + "\n")
        child.stdin.flush()
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
        identity = json.loads(ready.get(timeout=15))
        if spec.get("ready_path"):
            Path(spec["ready_path"]).write_text("ready", encoding="utf-8")
        while time.time() < identity["lease_until"] and child.poll() is None:
            time.sleep(0.05)
        store.revoke(spec["run_id"])  # Commits token revocation before process termination.
        if child.poll() is None:
            child.terminate()  # Uses the OS handle of the child we created, not an arbitrary PID.
        child.wait(timeout=10)
        target = (identity["pid"], identity["backend_start"], identity["application_name"])
        with psycopg.connect(os.environ["P0_POSTGRES_CLEANER_URL"], autocommit=True) as cleaner:
            remains = cleaner.execute(
                "SELECT public.p0_runner_exists(%s,%s,%s)", target
            ).fetchone()[0]
            if remains:
                assert cleaner.execute(
                    "SELECT public.p0_terminate_runner(%s,%s,%s)", target
                ).fetchone()[0]
            deadline = time.monotonic() + 5
            while cleaner.execute("SELECT public.p0_runner_exists(%s,%s,%s)", target).fetchone()[0]:
                if time.monotonic() > deadline:
                    raise RuntimeError("cannot verify dedicated connection release")
                time.sleep(0.05)
        run = store.read_run(spec["run_id"])
        with locked_saver(os.environ["P0_POSTGRES_URL"], run["session_id"]):
            assert store.claim(run["id"])
        result = {
            "mode": spec["mode"],
            "cleanup_required": remains,
            "after_expiry_seconds": max(0, time.time() - identity["lease_until"]),
            "takeover": True,
        }
        print(json.dumps(result))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        store.engine.dispose()


if __name__ == "__main__":
    main()
