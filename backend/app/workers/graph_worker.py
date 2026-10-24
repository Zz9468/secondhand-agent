"""P1 确定性 Worker；只调度队首和独立通知，不包含公开输入入口。"""

import argparse
import time

from sqlalchemy import or_, select

from app.db.models import ModelExecutionTask, ModelTaskStatus, NegotiationSession
from app.db.models.graph import NegotiationInput, NotificationDelivery
from app.persistence.checkpointer import ThreadBusy
from app.runtime.contracts import utcnow
from app.runtime.graph_runner import GraphRunner


def run_batch(factory, postgres_url, *, limit=20):
    runner = GraphRunner(factory, postgres_url)
    now = utcnow()
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
    result = {"completed": 0, "notified": 0, "busy": 0, "failed": 0}
    # 到期通知优先履行，即使当前输入无法从 checkpoint 恢复。
    for source in notifications:
        try:
            result["notified"] += int(runner.notify(source) is not None)
        except ThreadBusy:
            result["busy"] += 1
        except Exception:
            result["failed"] += 1
    for run_id in candidates:
        try:
            result["completed"] += int(runner.run(run_id) is not None)
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
    factory = graph_session_factory(settings.database_url)
    try:
        while True:
            result = run_batch(factory, settings.checkpoint_database_url, limit=args.batch_size)
            if args.once or any(result.values()):
                print(result, flush=True)
            if args.once:
                break
            time.sleep(1)
    finally:
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    main()
