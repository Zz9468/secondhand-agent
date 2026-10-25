"""Seller-owned recovery, serialized with runners and preserving all original budgets."""

from sqlalchemy import or_, select

from app.db.models import ModelExecutionTask, ModelTaskStatus, NegotiationSession, Product
from app.db.models.graph import (
    GraphRecoveryAction,
    ModelCallLedger,
    NegotiationInput,
    NotificationDelivery,
)
from app.persistence.checkpointer import locked_saver
from app.runtime.activity import used_ms
from app.runtime.contracts import Conflict, digest, thread_id, utcnow


class GraphRecoveryService:
    def __init__(self, factory, postgres_url, *, clock=utcnow):
        self.factory, self.postgres_url, self.clock = factory, postgres_url, clock

    def list(self, seller):
        with self.factory() as db:
            runs = list(
                db.execute(
                    select(NegotiationInput, ModelExecutionTask, Product)
                    .join(ModelExecutionTask, ModelExecutionTask.id == NegotiationInput.task_id)
                    .join(NegotiationSession, NegotiationSession.id == NegotiationInput.session_id)
                    .join(Product, Product.id == NegotiationSession.product_id)
                    .where(
                        Product.seller_id == seller,
                        NegotiationSession.archived_at.is_(None),
                        or_(
                            NegotiationInput.recovery_required.is_(True),
                            ModelExecutionTask.status.in_(
                                [ModelTaskStatus.FAILED, ModelTaskStatus.RETRY_WAIT]
                            ),
                        ),
                    )
                    .order_by(NegotiationInput.received_at.desc())
                    .limit(100)
                )
            )
            result = []
            for run, task, product in runs:
                calls = list(
                    db.scalars(select(ModelCallLedger).where(ModelCallLedger.input_id == run.id))
                )
                result.append(
                    {
                        "target": run.id,
                        "kind": "RUN",
                        "session_id": run.session_id,
                        "product_title": product.title,
                        "status": task.status.value,
                        "recovery_required": run.recovery_required,
                        "active_ms": used_ms(run, self.clock()),
                        "calls": len(calls),
                        "unknown_usage": sum(c.actual_tokens is None for c in calls),
                        "can_retry": True,
                        "can_terminate": True,
                    }
                )
            deliveries = db.execute(
                select(NotificationDelivery, Product)
                .outerjoin(NegotiationInput, NegotiationInput.id == NotificationDelivery.input_id)
                .join(
                    NegotiationSession,
                    or_(
                        NegotiationSession.id == NotificationDelivery.session_id,
                        NegotiationSession.id == NegotiationInput.session_id,
                    ),
                )
                .join(Product, Product.id == NegotiationSession.product_id)
                .where(
                    Product.seller_id == seller, NotificationDelivery.status == "MANUAL_REQUIRED"
                )
                .limit(100)
            )
            for item, product in deliveries:
                result.append(
                    {
                        "target": item.id,
                        "kind": "NOTIFICATION",
                        "session_id": item.session_id
                        or db.get(NegotiationInput, item.input_id).session_id,
                        "product_title": product.title,
                        "status": item.status,
                        "can_retry": True,
                        "can_terminate": False,
                    }
                )
            return result

    def act(self, seller, target, action, request_id, reason):
        if action not in {"RETRY", "TERMINATE", "NOTIFY"} or not reason.strip():
            raise Conflict("请选择恢复操作并填写原因")
        with self.factory() as db:
            run = db.get(NegotiationInput, target)
            delivery = db.get(NotificationDelivery, target) if not run else None
            if not run and not delivery:
                raise Conflict("恢复目标不存在或无权访问")
            sid = run.session_id if run else delivery.session_id
            if sid is None and delivery:
                sid = db.get(NegotiationInput, delivery.input_id).session_id
            session = db.get(NegotiationSession, sid)
            if db.get(Product, session.product_id).seller_id != seller:
                raise Conflict("恢复目标不存在或无权访问")
            tid = thread_id(sid) if run else delivery.source_thread_id
        with (
            locked_saver(self.postgres_url, tid, instance="seller-recovery"),
            self.factory() as db,
            db.begin(),
        ):
            now = self.clock()
            task = db.get(ModelExecutionTask, run.task_id, with_for_update=True) if run else None
            session = db.get(NegotiationSession, sid, with_for_update=True)
            if session.archived_at:
                raise Conflict("会话已归档")
            key = digest([seller, request_id])
            old = db.get(GraphRecoveryAction, key)
            if old:
                if (old.target, old.action, old.reason) != (target, action, reason):
                    raise Conflict("恢复请求载荷冲突")
                return {"status": "RECORDED", "action": old.action}
            if task:
                current = db.get(NegotiationInput, target)
                if current.input_seq != session.last_resolved_input_seq + 1:
                    raise Conflict("只能处置当前队首，不能回拨历史任务")
                if task.lease_expires_at and task.lease_expires_at > now:
                    raise Conflict("执行租约仍有效")
                if action == "NOTIFY" or task.status in {
                    ModelTaskStatus.SUCCEEDED,
                    ModelTaskStatus.CANCELLED,
                }:
                    raise Conflict("当前任务状态不支持该操作")
                task.status = ModelTaskStatus.PENDING
                task.max_attempts = max(task.max_attempts, task.attempt_count + 1)
                task.manual_retry_count += 1
                task.next_retry_at = None
                task.lease_token = task.lease_owner = task.lease_expires_at = None
                task.last_manual_action, task.last_manual_actor_id = action, seller
                task.last_manual_reason, task.last_manual_at = reason, now
                current.recovery_required = False
            else:
                item = db.get(NotificationDelivery, target, with_for_update=True)
                if action != "NOTIFY" or item.status != "MANUAL_REQUIRED":
                    raise Conflict("通知只能重试，不能丢弃已提交结果")
                item.status, item.next_retry_at = "PENDING", None
                item.lease_token = item.lease_until = None
            db.add(
                GraphRecoveryAction(
                    id=key,
                    session_id=sid,
                    actor_id=seller,
                    target=target,
                    action=action,
                    reason=reason,
                    created_at=now,
                )
            )
        return {"status": "RECORDED", "action": action}
