import json
import os
import subprocess
import sys
from dataclasses import asdict

import pytest
from sqlalchemy import func, select

from app.p0.contracts import Entry, RecoveryConflict, RunIdentity, can_confirm, choose_entry
from app.p0.graph import build_graph, new_turn
from app.p0.persistence import ThreadBusy, locked_saver
from app.p0.store import (
    InjectedFailure,
    StaleOwner,
    approvals,
    artifacts,
    attempts,
    messages,
    notifications,
    offers,
    outbox,
    receipts,
    sessions,
)


def execute(run, **spec):
    result = subprocess.run(
        [sys.executable, "-m", "scripts.p0_worker"],
        input=json.dumps({"run_id": run["id"], **spec}),
        text=True,
        capture_output=True,
        timeout=25,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in ("P0_POSTGRES_ADMIN_URL", "P0_POSTGRES_CLEANER_URL")
        },
    )
    if spec.get("fault"):
        assert result.returncode == 73, result.stderr
        return None
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def count(store, table, run):
    column = table.c.run_id if table in (artifacts, attempts) else table.c.source
    with store.engine.connect() as db:
        return db.execute(
            select(func.count()).select_from(table).where(column == run["id"])
        ).scalar_one()


def test_a25_display_causality():
    ack = dict(actor="buyer", offer_id="o1", offer_version=1, displayed_at=11, expires_at=20)
    args = dict(received_at=10, actor="buyer", offer_id="o1", offer_version=1)
    assert not can_confirm(frozen_ack=None, **args)
    assert not can_confirm(frozen_ack=ack, **args)
    assert can_confirm(frozen_ack=ack, **{**args, "received_at": 12})
    assert not can_confirm(frozen_ack=ack, **{**args, "received_at": 12, "actor": "other"})
    assert not can_confirm(frozen_ack=ack, **{**args, "received_at": 12, "offer_version": 2})


def test_a25_frozen_mysql_input_cannot_gain_later_display_authorization(store):
    sid = store.seed()
    early = store.enqueue(sid, "before-display", {"text": "行", "received_at": 10, "ack": None})
    ack = dict(actor="buyer", offer_id="o1", offer_version=1, displayed_at=11, expires_at=20)
    late = store.enqueue(sid, "after-display", {"text": "行", "received_at": 12, "ack": ack})
    for run, expected in ((early, False), (late, True)):
        payload = store.read_run(run["id"])["payload"]
        assert (
            can_confirm(
                received_at=payload["received_at"],
                frozen_ack=payload["ack"],
                actor="buyer",
                offer_id="o1",
                offer_version=1,
            )
            is expected
        )


def test_a27_fifo_retry_barrier(store, run):
    second = store.enqueue(run["session_id"], "request-2", {"text": "withdraw"})
    assert store.claim(run["id"])
    store.revoke(run["id"])
    assert store.claim(second["id"]) is None
    execute(run)
    assert store.claim(second["id"])


def test_a29_historical_task_never_rewinds(store, run):
    execute(run)
    second = store.enqueue(run["session_id"], "request-2", {"text": "next question"})
    outcome = execute(second)
    identity = RunIdentity(run["id"], run["seq"], run["payload_hash"])
    assert (
        choose_entry(identity, outcome["values"], (), has_evidence=True, is_head=False)
        == Entry.HISTORY
    )
    assert store.claim(run["id"]) is None
    assert count(store, offers, run) == 1


@pytest.mark.parametrize(
    "fault", ["before_graph", "after_model", "after_command", "after_publish", "after_end"]
)
def test_a16_a30_a60_real_process_crashes(store, run, fault):
    execute(run, fault=fault)
    if fault == "before_graph":
        assert count(store, artifacts, run) == 0
        assert count(store, receipts, run) == 0
    store.revoke(run["id"])
    outcome = execute(run)
    assert outcome["entry"] == (
        Entry.START
        if fault == "before_graph"
        else Entry.FINALIZE
        if fault == "after_end"
        else Entry.RESUME
    )
    assert outcome["pending"] == []
    for table in (
        offers,
        approvals,
        receipts,
        notifications,
        outbox,
        messages,
        artifacts,
        attempts,
    ):
        assert count(store, table, run) == 1, table.name
    with store.engine.connect() as db:
        row = (
            db.execute(select(sessions).where(sessions.c.id == run["session_id"])).mappings().one()
        )
        assert row["cursor"] == 1 and row["rounds"] == 1


@pytest.mark.parametrize("fault", range(1, 7))
def test_a31_every_combination_write_rolls_back(store, run, fault):
    owner = store.claim(run["id"])
    with pytest.raises(InjectedFailure):
        store.command(run["id"], *owner, fail_at=fault)
    for table in (offers, approvals, receipts, notifications, outbox):
        assert count(store, table, run) == 0
    with store.engine.connect() as db:
        row = (
            db.execute(select(sessions).where(sessions.c.id == run["session_id"])).mappings().one()
        )
        assert row["rounds"] == 0 and row["version"] == 1
    first = store.command(run["id"], *owner)
    assert store.command(run["id"], *owner) == first
    with pytest.raises(ValueError, match="payload conflict"):
        store.command(run["id"], *owner, amount="801.00")


def test_two_turns_reset_transient_fields(store, run, pg_url):
    execute(run)
    second = store.enqueue(run["session_id"], "request-2", {"text": "next query"})
    with locked_saver(pg_url, run["session_id"]) as (saver, _):
        owner = store.claim(second["id"])
        graph = build_graph(saver, store, owner)
        config = {"configurable": {"thread_id": run["session_id"]}}
        graph.invoke(new_turn(second), config, durability="sync")
        history = list(graph.get_state_history(config))
        first_step = [
            s
            for s in history
            if s.values.get("run_id") == second["id"] and s.next == ("query_product",)
        ][0]
        assert first_step.values["receipt"] == {}
        assert first_step.values["model_result"] == {}
        assert first_step.values["review_count"] == 0
        assert first_step.values["intents"] == []


def test_interrupt_released_connection_and_fresh_process_resume(store, run, pg_url):
    result = execute(run, approval=True)
    assert result["pending"] == ["approval"]
    with locked_saver(pg_url, run["session_id"]):
        pass
    result = execute(run, approval=True, resume={"approved": True})
    assert result["values"]["approved"] is True
    assert count(store, receipts, run) == 1


def test_single_connection_lock_and_worker_privileges(pg_url):
    import psycopg

    with locked_saver(pg_url, "mutual-exclusion") as (saver, connection):
        assert saver.conn is connection
        with pytest.raises(ThreadBusy), locked_saver(pg_url, "mutual-exclusion"):
            pass
        with psycopg.connect(pg_url) as independent:
            row = independent.execute(
                "SELECT rolsuper, rolcreaterole, rolcreatedb, "
                "pg_has_role(current_user, 'pg_signal_backend', 'MEMBER') "
                "FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            assert row == (False, False, False, False)
            assert not independent.execute(
                "SELECT has_function_privilege(current_user, "
                "'pg_catalog.pg_terminate_backend(integer,bigint)', 'EXECUTE')"
            ).fetchone()[0]


def test_a33_late_model_is_audit_only(store, run):
    owner = store.claim(run["id"])

    def late():
        store.revoke(run["id"])
        return {"intent": "INVALID_LATE_RESULT"}

    with pytest.raises(StaleOwner):
        store.model_artifact(run["id"], *owner, late)
    assert count(store, artifacts, run) == 0
    assert count(store, attempts, run) == 1
    with pytest.raises(StaleOwner):
        store.command(run["id"], *owner)


def test_a46_missing_or_mismatched_checkpoint_holds_barrier():
    identity = RunIdentity("run", 1, "hash")
    with pytest.raises(RecoveryConflict):
        choose_entry(identity, {}, (), has_evidence=True, is_head=True)
    with pytest.raises(RecoveryConflict):
        choose_entry(
            identity,
            {**asdict(identity), "input_hash": "other"},
            ("node",),
            has_evidence=True,
            is_head=True,
        )


def test_a45_duplicate_input_freezes_display_ack(store, run):
    assert store.enqueue(run["session_id"], "request-1", run["payload"])["id"] == run["id"]
    with pytest.raises(ValueError, match="payload conflict"):
        store.enqueue(run["session_id"], "request-1", {**run["payload"], "ack": "new"})


def test_checkpoint_connection_loss_stops_no_reconnect(pg_url):
    import psycopg

    with locked_saver(pg_url, "disconnect") as (saver, connection):
        with psycopg.connect(os.environ["P0_POSTGRES_ADMIN_URL"], autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (connection.info.backend_pid,))
        with pytest.raises(psycopg.OperationalError):
            saver.get_tuple({"configurable": {"thread_id": "disconnect"}})
        assert connection.closed or connection.broken
    with locked_saver(pg_url, "disconnect"):
        pass
