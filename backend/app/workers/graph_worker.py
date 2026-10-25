"""P1 确定性 Worker；只调度队首和独立通知，不包含公开输入入口。"""

import argparse
import time

from sqlalchemy import or_, select

from app.db.models import ModelExecutionTask, ModelTaskStatus, NegotiationSession, NegotiationStatus
from app.db.models.graph import (
    ApprovalExecution,
    NegotiationInput,
    NotificationDelivery,
    OutboxEvent,
)
from app.persistence.checkpointer import ThreadBusy
from app.runtime.approval_runner import ApprovalRunner
from app.runtime.contracts import utcnow
from app.runtime.graph_runner import GraphRunner
from app.services.graph_approval_service import GraphApprovalService


def run_batch(factory, postgres_url, *, limit=20, progress=lambda: None):
    runner = GraphRunner(factory, postgres_url)
    now = utcnow()
    result = {
        "completed": 0,
        "notified": 0,
        "approvals": 0,
        "reconciled": 0,
        "busy": 0,
        "failed": 0,
    }
    lifecycle = GraphApprovalService(factory)
    with factory() as db:
        sessions = list(
            db.scalars(
                select(NegotiationSession.id)
                .where(
                    NegotiationSession.graph_version.is_not(None),
                    NegotiationSession.current_offer_id.is_not(None),
                    NegotiationSession.status.in_(
                        [NegotiationStatus.ACTIVE, NegotiationStatus.WAITING_APPROVAL]
                    ),
                )
                .order_by(NegotiationSession.lifecycle_checked_at, NegotiationSession.id)
                .limit(limit)
            )
        )
    for sid in sessions:
        progress()
        try:
            lifecycle.reconcile_offer(sid)
            result["reconciled"] += 1
        except Exception:
            result["failed"] += 1
    with factory() as db:
        candidates = list(
            db.scalars(
                select(NegotiationInput.id)
                .join(
                    NegotiationSession,
                    NegotiationInput.session_id == NegotiationSession.id,
                )
                .join(ModelExecutionTask, NegotiationInput.task_id == ModelExecutionTask.id)
                .where(
                    NegotiationInput.input_seq == NegotiationSession.last_resolved_input_seq + 1,
                    NegotiationInput.recovery_required.is_(False),
                    or_(
                        ModelExecutionTask.lease_expires_at.is_(None),
                        ModelExecutionTask.lease_expires_at <= now,
                    ),
                    or_(
                        ModelExecutionTask.next_retry_at.is_(None),
                        ModelExecutionTask.next_retry_at <= now,
                    ),
                    ModelExecutionTask.status.in_(
                        [
                            ModelTaskStatus.PENDING,
                            ModelTaskStatus.RETRY_WAIT,
                            ModelTaskStatus.RUNNING,
                        ]
                    ),
                )
                .order_by(NegotiationInput.received_at)
                .limit(limit)
            )
        )
        notifications = list(
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
                .limit(limit)
            )
        )
        approvals = list(
            db.scalars(
                select(ApprovalExecution.approval_id)
                .where(
                    ApprovalExecution.recovery_required.is_(False),
                    or_(
                        ApprovalExecution.lease_until.is_(None),
                        ApprovalExecution.lease_until <= now,
                    ),
                    or_(
                        ApprovalExecution.next_retry_at.is_(None),
                        ApprovalExecution.next_retry_at <= now,
                    ),
                    select(OutboxEvent.id)
                    .where(
                        OutboxEvent.approval_id == ApprovalExecution.approval_id,
                        OutboxEvent.status == "PENDING",
                    )
                    .exists(),
                )
                .order_by(ApprovalExecution.approval_id)
                .limit(limit)
            )
        )
    # 到期通知优先履行，即使当前输入无法从 checkpoint 恢复。
    for source in notifications:
        progress()
        try:
            result["notified"] += int(runner.notify(source) is not None)
        except ThreadBusy:
            result["busy"] += 1
        except Exception:
            result["failed"] += 1
    for run_id in candidates:
        progress()
        try:
            if runner.queue.read(run_id)["graph_version"] == "negotiation-v3":
                from app.runtime.supervisor import run_supervised

                completed = run_supervised(factory, postgres_url, run_id, progress=progress)
                result["completed"] += int(completed)
                result["failed"] += int(not completed)
            else:
                result["completed"] += int(runner.run(run_id) is not None)
        except ThreadBusy:
            result["busy"] += 1
        except Exception:
            result["failed"] += 1
    for approval_id in approvals:
        progress()
        try:
            result["approvals"] += int(
                ApprovalRunner(factory, postgres_url).run(approval_id) is not None
            )
        except ThreadBusy:
            result["busy"] += 1
        except Exception:
            result["failed"] += 1
    return result


def main():
    from app.core.config import get_settings
    from app.runtime.database import graph_session_factory

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.checkpoint_database_url:
        raise RuntimeError("checkpoint 数据库未配置")
    if not 1 <= args.batch_size <= 200:
        parser.error("batch-size 必须在 1 到 200 之间")
    from app.runtime.operations import instance_id, pulse

    factory = graph_session_factory(settings.database_url)
    identity = instance_id()

    def progress():
        pulse(factory, "worker", identity)

    try:
        while True:
            progress()
            result = run_batch(
                factory, settings.checkpoint_database_url, limit=args.batch_size, progress=progress
            )
            if args.once or any(result.values()):
                print(result, flush=True)
            if args.once:
                break
            time.sleep(1)
    finally:
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    main()
