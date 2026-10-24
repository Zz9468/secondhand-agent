import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import insert, select
from sqlalchemy.exc import OperationalError

from app.p0.graph import ProbeState, build_graph, new_turn
from app.p0.persistence import locked_saver
from app.p0.store import InjectedFailure, inputs, offers


@pytest.mark.parametrize("mode", ["hang", "exit", "partition"])
def test_a32_independent_supervisor(store, run, pg_url, tmp_path, record_property, mode):
    spec = {"run_id": run["id"], "mode": mode, "lease_seconds": 30 if mode == "hang" else 2}
    stop, partition = threading.Event(), threading.Event()
    sockets = []
    if mode == "partition":
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen()
        sockets.append(server)
        spec["proxy_port"] = server.getsockname()[1]
        spec["ready_path"] = str(tmp_path / "ready")

        def pump(source, target):
            try:
                source.settimeout(0.1)
                while not stop.is_set():
                    try:
                        data = source.recv(65536)
                    except TimeoutError:
                        continue
                    if not data:
                        # Partition deliberately keeps the upstream DB socket alive
                        # after worker termination, requiring restricted cleanup.
                        if partition.is_set():
                            stop.wait(8)
                        return
                    if not partition.is_set():
                        target.sendall(data)
            except OSError:
                pass

        def proxy():
            client, _ = server.accept()
            upstream = socket.create_connection(
                ("127.0.0.1", int(conninfo_to_dict(pg_url)["port"]))
            )
            sockets.extend([client, upstream])
            for source, target in ((client, upstream), (upstream, client)):
                threading.Thread(target=pump, args=(source, target), daemon=True).start()

        threading.Thread(target=proxy, daemon=True).start()
    process = subprocess.Popen(
        [sys.executable, "-m", "scripts.p0_supervisor"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        process.stdin.write(json.dumps(spec))
        process.stdin.close()
        process.stdin = None
        if mode == "partition":
            deadline = time.monotonic() + 15
            while not (tmp_path / "ready").exists():
                assert time.monotonic() < deadline
                time.sleep(0.05)
            partition.set()
        stdout, stderr = process.communicate(timeout=50)
        assert process.returncode == 0, stderr
        result = json.loads(stdout)
        assert result["takeover"] and result["after_expiry_seconds"] < 90
        if mode == "partition":
            assert result["cleanup_required"]
        record_property("takeover", json.dumps(result))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        stop.set()
        for stream in sockets:
            stream.close()


def test_cleanup_rejects_mismatched_identity(pg_url):
    with locked_saver(pg_url, "protected", instance="protected") as (_, worker):
        row = worker.execute(
            "SELECT pid,backend_start,application_name FROM pg_stat_activity "
            "WHERE pid=pg_backend_pid()"
        ).fetchone()
        with psycopg.connect(os.environ["P0_POSTGRES_CLEANER_URL"], autocommit=True) as cleaner:
            assert not cleaner.execute(
                "SELECT public.p0_terminate_runner(%s,%s,%s)",
                (row["pid"], row["backend_start"], "wrong"),
            ).fetchone()[0]
        assert worker.execute("SELECT 1 AS alive").fetchone()["alive"] == 1


def test_mysql_timeout_rolls_back_all_writes_and_releases_owner(store, run):
    owner = store.claim(run["id"])
    with store.engine.connect() as blocker:
        transaction = blocker.begin()
        blocker.execute(select(inputs).where(inputs.c.id == run["id"]).with_for_update())

        def contender():
            with pytest.raises(OperationalError), store.engine.begin() as db:
                db.exec_driver_sql("SET SESSION innodb_lock_wait_timeout=1")
                db.execute(insert(offers).values(source=run["id"], amount=800))
                store.owner(db, run["id"], *owner)

        start = time.monotonic()
        with ThreadPoolExecutor(1) as executor:
            executor.submit(contender).result(timeout=8)
        assert time.monotonic() - start < 5
        transaction.rollback()
    with store.engine.connect() as db:
        assert not db.execute(select(offers).where(offers.c.source == run["id"])).first()
    store.revoke(run["id"])
    assert store.claim(run["id"])


def test_server_query_deadline_rolls_back_and_releases_owner(store, run):
    owner = store.claim(run["id"])
    start = time.monotonic()
    with pytest.raises((InjectedFailure, OperationalError)), store.engine.begin() as db:
        store.owner(db, run["id"], *owner)
        db.execute(insert(offers).values(source=run["id"], amount=800))
        # The server interrupts SLEEP itself, rather than only cancelling a Python future.
        db.exec_driver_sql("SELECT /*+ MAX_EXECUTION_TIME(500) */ SLEEP(8)")
        raise InjectedFailure("bounded SQL operation timed out; abort whole command")
    assert time.monotonic() - start < 3
    store.revoke(run["id"])
    assert store.claim(run["id"])
    with store.engine.connect() as db:
        assert not db.execute(select(offers).where(offers.c.source == run["id"])).first()


def test_pending_writes_do_not_repeat_successful_sibling(store, run, pg_url):
    calls = []

    def good(state):
        calls.append("good")
        return {"product": "read once"}

    def bad(state):
        if calls.count("bad") == 0:
            calls.append("bad")
            raise InjectedFailure("parallel sibling failure")
        return {"approved": True}

    def graph(saver):
        builder = StateGraph(ProbeState)
        builder.add_node("good", good).add_node("bad", bad)
        builder.add_edge(START, "good").add_edge(START, "bad")
        builder.add_edge("good", END).add_edge("bad", END)
        return builder.compile(checkpointer=saver)

    config = {"configurable": {"thread_id": run["session_id"]}}
    with locked_saver(pg_url, run["session_id"]) as (saver, _):
        with pytest.raises(InjectedFailure):
            graph(saver).invoke(new_turn(run), config, durability="sync")
        assert saver.get_tuple(config).pending_writes
    with locked_saver(pg_url, run["session_id"]) as (saver, _):
        result = graph(saver).invoke(None, config, durability="sync")
    assert result["product"] == "read once" and result["approved"]
    assert calls.count("good") == 1


def test_initial_sync_checkpoint_failure_prevents_side_effects(store, run, pg_url):
    class FailingSaver(PostgresSaver):
        def put(self, *args, **kwargs):
            raise InjectedFailure("initial checkpoint unavailable")

    with locked_saver(pg_url, run["session_id"]) as (_, connection):
        owner = store.claim(run["id"])
        graph = build_graph(FailingSaver(connection), store, owner)
        with pytest.raises(InjectedFailure):
            graph.invoke(
                new_turn(run), {"configurable": {"thread_id": run["session_id"]}}, durability="sync"
            )
    with store.engine.connect() as db:
        assert not db.execute(select(offers).where(offers.c.source == run["id"])).first()
