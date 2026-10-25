"""独立监督进程使用精确连接身份清理；撤销所有权必须先于终止连接。"""

from datetime import timedelta
from uuid import uuid4

import psycopg
from sqlalchemy import select

from app.db.models import ModelExecutionTask, ModelTaskStatus
from app.db.models.graph import NegotiationInput
from app.db.models.operations import GraphExecutionInstance, GraphServiceHeartbeat
from app.runtime.activity import settle
from app.runtime.contracts import utcnow
from app.runtime.input_queue import bounded_transaction, lock_owner


def register_instance(factory, run_id, instance, connection):
    identity = connection.execute(
        "SELECT pg_backend_pid() AS pid, backend_start::text, application_name "
        "FROM pg_stat_activity WHERE pid=pg_backend_pid()"
    ).fetchone()
    with factory() as db, db.begin():
        bounded_transaction(db)
        db.add(
            GraphExecutionInstance(
                id=instance,
                input_id=run_id,
                pg_pid=identity["pid"],
                pg_backend_start=identity["backend_start"],
                application_name=identity["application_name"],
                registered_at=utcnow(),
                state="REGISTERED",
            )
        )


def attach_owner(factory, run_id, instance, owner):
    with factory() as db, db.begin():
        lock_owner(db, run_id, owner, utcnow())
        row = db.get(GraphExecutionInstance, instance, with_for_update=True)
        if row.state != "REGISTERED":
            raise RuntimeError("execution registration already revoked")
        row.lease_token, row.ownership_epoch = owner


def cleanup_instance(factory, cleaner_url, instance, *, force=False):
    now = utcnow()
    with factory() as db, db.begin():
        bounded_transaction(db)
        record = db.get(GraphExecutionInstance, instance)
        if not record or record.state == "RELEASED":
            return False
        run = db.get(NegotiationInput, record.input_id)
        task = db.get(ModelExecutionTask, run.task_id, with_for_update=True)
        run = db.get(
            NegotiationInput, record.input_id, with_for_update=True, populate_existing=True
        )
        record = db.get(
            GraphExecutionInstance, instance, with_for_update=True, populate_existing=True
        )
        same_owner = (record.lease_token, record.ownership_epoch) == (
            task.lease_token,
            task.ownership_epoch,
        ) and record.lease_token is not None
        active = task.lease_expires_at and task.lease_expires_at > now
        overdue = (run.closing_deadline and run.closing_deadline <= now) or (
            now >= record.registered_at + timedelta(seconds=150)
        )
        # An instance may be between connection registration and claiming the input.
        if not force and record.state == "REGISTERED":
            if record.lease_token is None and now < record.registered_at + timedelta(seconds=30):
                return False
            if same_owner and active and not overdue:
                return False
        if same_owner and task.status == ModelTaskStatus.RUNNING:
            settle(run, now)
            task.ownership_epoch += 1
            task.lease_token = task.lease_owner = task.lease_expires_at = None
            task.status = ModelTaskStatus.RETRY_WAIT
            task.next_retry_at = now
        record.state, record.checked_at = "REVOKED", now
        target = (record.pg_pid, record.pg_backend_start, record.application_name)
    # Separate committed transaction: stale business writes are fenced before PG termination.
    try:
        with psycopg.connect(
            cleaner_url, autocommit=True, connect_timeout=3, options="-c statement_timeout=5000"
        ) as db:
            exists = db.execute("SELECT public.graph_runner_exists(%s,%s,%s)", target).fetchone()[0]
            if exists:
                db.execute("SELECT public.graph_terminate_runner(%s,%s,%s)", target)
            if db.execute("SELECT public.graph_runner_exists(%s,%s,%s)", target).fetchone()[0]:
                raise RuntimeError("connection_release_unverified")
    except Exception as exc:
        with factory() as db, db.begin():
            row = db.get(GraphExecutionInstance, instance, with_for_update=True)
            row.error_category = type(exc).__name__[:64]
        raise RuntimeError("checkpoint_cleanup_failed") from None
    with factory() as db, db.begin():
        row = db.get(GraphExecutionInstance, instance, with_for_update=True)
        row.state, row.checked_at, row.error_category = "RELEASED", utcnow(), None
    return True


def sweep(factory, cleaner_url, *, limit=100):
    with factory() as db:
        ids = list(
            db.scalars(
                select(GraphExecutionInstance.id)
                .where(GraphExecutionInstance.state != "RELEASED")
                .order_by(GraphExecutionInstance.checked_at, GraphExecutionInstance.registered_at)
                .limit(limit)
            )
        )
    result = {"released": 0, "failed": 0}
    for instance in ids:
        try:
            result["released"] += int(cleanup_instance(factory, cleaner_url, instance))
        except Exception:
            result["failed"] += 1
    return result


def pulse(factory, service, instance, status="ok"):
    with factory() as db, db.begin():
        bounded_transaction(db)
        db.merge(
            GraphServiceHeartbeat(id=instance, service=service, last_seen=utcnow(), status=status)
        )


def service_health(factory):
    with factory() as db:
        rows = list(
            db.scalars(
                select(GraphServiceHeartbeat).where(
                    GraphServiceHeartbeat.last_seen >= utcnow() - timedelta(seconds=20)
                )
            )
        )
    return {
        service: "ok"
        if any(r.service == service and r.status == "ok" for r in rows)
        else "unavailable"
        for service in ("worker", "watchdog")
    }


def instance_id():
    return uuid4().hex
