"""P6 真实数据库运维边界；不调用真实模型。"""

import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.api.dependencies import get_current_user, get_session_factory_dependency
from app.core.config import get_settings
from app.db.models import ModelExecutionTask
from app.db.models.operations import GraphExecutionInstance
from app.main import create_app
from app.persistence.checkpointer import lock_key, locked_saver
from app.runtime.contracts import StaleOwner, thread_id, utcnow
from app.runtime.operations import (
    attach_owner,
    cleanup_instance,
    pulse,
    register_instance,
    service_health,
)
from tests.graph.test_business import submit


def connection(factory, run, instance):
    db = psycopg.connect(
        os.environ["P0_POSTGRES_URL"],
        autocommit=True,
        row_factory=dict_row,
        application_name=f"secondhand-p0:graph-{instance}",
    )
    register_instance(factory, run, instance, db)
    return db


def test_cleanup_revokes_expired_owner_and_verifies_release(case, record_property):
    factory, sid, buyer, clock, queue, _ = case
    run = queue.enqueue(sid, buyer, "p6-cleanup", submit())
    instance = uuid4().hex
    db = connection(factory, run, instance)
    try:
        db.execute("SELECT pg_advisory_lock(%s)", (lock_key(thread_id(sid)),))
        owner = queue.claim(run)
        attach_owner(factory, run, instance, owner)
        assert not cleanup_instance(factory, os.environ["P0_POSTGRES_CLEANER_URL"], instance)
        with factory() as tx, tx.begin():
            task = tx.get(ModelExecutionTask, queue.read(run)["task_id"])
            task.lease_expires_at = utcnow() - timedelta(seconds=1)
        start = utcnow()
        assert cleanup_instance(factory, os.environ["P0_POSTGRES_CLEANER_URL"], instance)
        elapsed = (utcnow() - start).total_seconds()
        record_property("expired_to_lock_available_seconds", elapsed)
        assert elapsed < 90
        with pytest.raises(StaleOwner):
            queue.heartbeat(run, owner)
        with locked_saver(os.environ["P0_POSTGRES_URL"], thread_id(sid)):
            clock.advance(2)
            assert queue.claim(run)
        with factory() as tx:
            row = tx.get(GraphExecutionInstance, instance)
            assert row.state == "RELEASED" and row.error_category is None
    finally:
        db.close()


def test_restricted_cleaner_rejects_mismatched_identity_and_worker_has_no_kill(case):
    factory, sid, buyer, _, queue, _ = case
    run = queue.enqueue(sid, buyer, "p6-exact-identity", submit())
    instance = uuid4().hex
    with connection(factory, run, instance) as db:
        with factory() as tx:
            row = tx.get(GraphExecutionInstance, instance)
        target = (row.pg_pid, row.pg_backend_start, row.application_name)
        with psycopg.connect(os.environ["P0_POSTGRES_CLEANER_URL"], autocommit=True) as cleaner:
            assert cleaner.execute("SELECT graph_runner_exists(%s,%s,%s)", target).fetchone()[0]
            for wrong in (
                (row.pg_pid, "2000-01-01T00:00:00Z", row.application_name),
                (row.pg_pid, row.pg_backend_start, "secondhand-p0:graph-" + "0" * 32),
            ):
                assert not cleaner.execute(
                    "SELECT graph_terminate_runner(%s,%s,%s)", wrong
                ).fetchone()[0]
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cleaner.execute("SELECT pg_terminate_backend(%s)", (row.pg_pid,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("SELECT graph_terminate_runner(%s,%s,%s)", target)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("SELECT pg_terminate_backend(%s)", (row.pg_pid,))
        assert db.execute("SELECT 1 AS ok").fetchone()["ok"] == 1


def test_cleanup_failure_remains_visible_and_retryable(case):
    factory, sid, buyer, _, queue, _ = case
    run = queue.enqueue(sid, buyer, "p6-failure", submit())
    instance = uuid4().hex
    with connection(factory, run, instance):
        owner = queue.claim(run)
        attach_owner(factory, run, instance, owner)
        with pytest.raises(RuntimeError, match="checkpoint_cleanup_failed"):
            cleanup_instance(
                factory, "host=127.0.0.1 port=1 connect_timeout=1", instance, force=True
            )
        with factory() as db:
            row = db.get(GraphExecutionInstance, instance)
            assert row.state == "REVOKED" and row.error_category
        assert cleanup_instance(factory, os.environ["P0_POSTGRES_CLEANER_URL"], instance)


def test_graph_only_api_never_calls_legacy_runtime_and_disabled_intake_fails_closed(case):
    factory, sid, buyer, _, _, _ = case
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=buyer)
    app.dependency_overrides[get_session_factory_dependency] = lambda: factory
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"graph_chat_enabled": False}
    )
    with TestClient(app) as client:
        assert client.post("/api/negotiations", json={"product_id": 1001}).status_code == 503
        assert (
            client.post(
                f"/api/negotiations/{sid}/messages",
                json={"content": "900", "request_id": "old-contract-001"},
            ).status_code
            == 422
        )
        assert client.get("/api/seller/model-tasks").status_code == 404


def test_service_health_requires_recent_progress(graph_factory):
    from app.db.models.operations import GraphServiceHeartbeat

    first, second = uuid4().hex, uuid4().hex
    pulse(graph_factory, "worker", first)
    pulse(graph_factory, "watchdog", second)
    assert service_health(graph_factory) == {"worker": "ok", "watchdog": "ok"}
    with graph_factory() as db, db.begin():
        db.get(GraphServiceHeartbeat, first).last_seen = utcnow() - timedelta(seconds=21)
    assert service_health(graph_factory)["worker"] == "unavailable"


def test_cleanup_old_instance_cannot_revoke_new_owner(case):
    factory, sid, buyer, clock, queue, _ = case
    run = queue.enqueue(sid, buyer, "p6-old-instance", submit())
    instance = uuid4().hex
    with connection(factory, run, instance):
        owner = queue.claim(run)
        attach_owner(factory, run, instance, owner)
        queue.release(run, owner)
    clock.advance(2)
    new_owner = queue.claim(run)
    assert new_owner != owner
    assert cleanup_instance(factory, os.environ["P0_POSTGRES_CLEANER_URL"], instance, force=True)
    queue.heartbeat(run, new_owner)
