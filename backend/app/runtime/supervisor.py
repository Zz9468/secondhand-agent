"""Bound execution even when the SDK ignores its timeout and still holds the thread lock."""

import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import or_, select

from app.db.models import ModelExecutionTask, ModelTaskStatus
from app.db.models.graph import NegotiationInput, NotificationDelivery
from app.db.models.operations import GraphExecutionInstance
from app.persistence.checkpointer import locked_saver
from app.runtime.activity import settle
from app.runtime.contracts import thread_id, utcnow
from app.runtime.input_queue import bounded_transaction
from app.runtime.operations import instance_id


def dispatch_due_notifications(factory, postgres_url):
    from app.runtime.graph_runner import GraphRunner

    now = utcnow()
    with factory() as db:
        sources = list(
            db.scalars(
                select(NotificationDelivery.id)
                .where(
                    NotificationDelivery.status.in_(["PENDING", "RETRY_WAIT", "RUNNING"]),
                    NotificationDelivery.reply_deadline <= now,
                    or_(
                        NotificationDelivery.lease_until.is_(None),
                        NotificationDelivery.lease_until <= now,
                    ),
                    or_(
                        NotificationDelivery.next_retry_at.is_(None),
                        NotificationDelivery.next_retry_at <= now,
                    ),
                )
                .order_by(NotificationDelivery.reply_deadline)
                .limit(10)
            )
        )
    runner = GraphRunner(factory, postgres_url)
    for source in sources:
        try:
            runner.notify(source)
        except Exception:
            # Each delivery owns its durable retry state, including a lock still held by a runner.
            continue


def run_supervised(
    factory, postgres_url, run_id, *, module="app.workers.graph_execution", progress=lambda: None
):
    instance = instance_id()
    env = {
        **{
            k: v
            for k, v in os.environ.items()
            if not any(
                word in k
                for word in (
                    "ADMIN",
                    "CLEANER",
                    "ROOT_PASSWORD",
                    "POSTGRES_PASSWORD",
                    "CHECKPOINT_PASSWORD",
                )
            )
        },
        "GRAPH_EXECUTION_INSTANCE": instance,
        "GRAPH_EXECUTION_DATABASE_URL": factory.kw["bind"].url.render_as_string(
            hide_password=False
        ),
        "GRAPH_EXECUTION_POSTGRES_URL": postgres_url,
        "GRAPH_EXECUTION_RUN_ID": run_id,
    }
    process = subprocess.Popen(
        [sys.executable, "-m", module],
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    hard_deadline = time.monotonic() + 150
    killed = False
    next_notification_poll = 0
    try:
        while process.poll() is None:
            if time.monotonic() >= next_notification_poll:
                progress()
                dispatch_due_notifications(factory, postgres_url)
                next_notification_poll = time.monotonic() + 1
            with factory() as db:
                run = db.get(NegotiationInput, run_id)
                delivery = db.get(NotificationDelivery, f"turn:{run_id}:reply")
                deadline = run.closing_deadline
                if not deadline and run.active_since:
                    deadline = run.active_since + timedelta(
                        seconds=max(0, run.config.get("active_seconds", 120) - run.active_ms / 1000)
                        + 15
                    )
                if delivery:
                    deadline = (
                        min(deadline, delivery.reply_deadline + timedelta(seconds=15))
                        if deadline
                        else delivery.reply_deadline + timedelta(seconds=15)
                    )
                task = db.get(ModelExecutionTask, run.task_id)
                lease_lost = (
                    task.status == ModelTaskStatus.RUNNING
                    and task.lease_expires_at
                    and task.lease_expires_at <= utcnow()
                )
                expired = (deadline and utcnow() >= deadline) or lease_lost
            if expired or time.monotonic() >= hard_deadline:
                with factory() as db, db.begin():
                    bounded_transaction(db)
                    task = db.get(ModelExecutionTask, run.task_id, with_for_update=True)
                    current = db.get(NegotiationInput, run_id, with_for_update=True)
                    record = db.get(GraphExecutionInstance, instance)
                    if (
                        record
                        and task.status == ModelTaskStatus.RUNNING
                        and (task.lease_token, task.ownership_epoch)
                        == (record.lease_token, record.ownership_epoch)
                    ):
                        settle(current, utcnow())
                        task.ownership_epoch += 1
                        task.lease_token = task.lease_owner = task.lease_expires_at = None
                        task.status = ModelTaskStatus.RETRY_WAIT
                        task.next_retry_at = utcnow()
                process.kill()
                process.wait(timeout=5)
                killed = True
                break
            time.sleep(0.25)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    if killed:
        # Never bypass a surviving PostgreSQL session. Failure here is visible to the worker.
        with locked_saver(postgres_url, thread_id(run.session_id), instance="takeover-check"):
            pass
    from app.runtime.graph_runner import GraphRunner

    with factory() as db:
        delivery = db.get(NotificationDelivery, f"turn:{run_id}:reply")
    if delivery and delivery.status != "PUBLISHED":
        GraphRunner(factory, postgres_url).notify(delivery.id)
    return process.returncode == 0
