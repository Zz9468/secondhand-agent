"""Conservative session archive: tombstone first, checkpoint cleanup second, audit retained."""

from contextlib import ExitStack
from datetime import timedelta

from sqlalchemy import func, select

from app.db.models import (
    ApprovalRequest,
    Message,
    ModelExecutionTask,
    ModelTaskStatus,
    NegotiationSession,
    NegotiationStatus,
)
from app.db.models.graph import (
    ApprovalExecution,
    NegotiationInput,
    NotificationDelivery,
    OutboxEvent,
    PublicInteractionContext,
)
from app.persistence.checkpointer import locked_saver
from app.runtime.contracts import Conflict, thread_id, utcnow
from app.services.approval_lifecycle import approval_thread


class GraphRetentionService:
    def __init__(self, factory, postgres_url, *, clock=utcnow):
        self.factory, self.postgres_url, self.clock = factory, postgres_url, clock

    def check(self, db, sid):
        session = db.get(NegotiationSession, sid, with_for_update=True)
        now = self.clock()
        if not session or not session.graph_version:
            raise Conflict("不是图会话")
        if session.archived_at:
            return session
        if (
            session.status not in {NegotiationStatus.AGREED, NegotiationStatus.CLOSED}
            or session.last_resolved_input_seq != session.next_input_seq - 1
        ):
            raise Conflict("活跃线程或未完成输入不能归档")
        cutoff = now - timedelta(days=90)
        times = [session.updated_at]
        for model, field in (
            (Message, Message.created_at),
            (ModelExecutionTask, ModelExecutionTask.updated_at),
            (ApprovalRequest, ApprovalRequest.updated_at),
        ):
            last = db.scalar(select(func.max(field)).where(model.session_id == sid))
            if last:
                times.append(last)
        if max(times) > cutoff:
            raise Conflict("终态数据至少保留90天")
        runs = select(NegotiationInput.id).where(NegotiationInput.session_id == sid)
        approvals = select(ApprovalRequest.id).where(ApprovalRequest.session_id == sid)
        if db.scalar(
            select(ApprovalRequest.id)
            .where(
                ApprovalRequest.session_id == sid,
                (ApprovalRequest.status == "PENDING")
                | (ApprovalRequest.effect_status == "NOT_APPLIED"),
            )
            .limit(1)
        ):
            raise Conflict("等待决定或应用的审批不能归档")
        deliveries = select(NotificationDelivery.id).where(
            (NotificationDelivery.session_id == sid) | NotificationDelivery.input_id.in_(runs)
        )
        if db.scalar(
            select(NotificationDelivery.id)
            .where(
                NotificationDelivery.id.in_(deliveries), NotificationDelivery.status != "PUBLISHED"
            )
            .limit(1)
        ):
            raise Conflict("待通知回执必须保留")
        if db.scalar(
            select(ModelExecutionTask.id)
            .where(
                ModelExecutionTask.session_id == sid,
                ModelExecutionTask.status.not_in(
                    [ModelTaskStatus.SUCCEEDED, ModelTaskStatus.CANCELLED]
                ),
            )
            .limit(1)
        ):
            raise Conflict("恢复所需任务必须保留")
        if db.scalar(
            select(PublicInteractionContext.id)
            .where(
                PublicInteractionContext.session_id == sid,
                PublicInteractionContext.expires_at > now,
            )
            .limit(1)
        ):
            raise Conflict("有效展示凭据必须保留")
        if db.scalar(
            select(OutboxEvent.id)
            .where(
                (OutboxEvent.delivery_id.in_(deliveries) | OutboxEvent.approval_id.in_(approvals)),
                OutboxEvent.status == "PENDING",
            )
            .limit(1)
        ):
            raise Conflict("待处理事件必须保留")
        if db.scalar(
            select(ApprovalExecution.approval_id)
            .where(
                ApprovalExecution.approval_id.in_(approvals),
                (
                    ApprovalExecution.recovery_required.is_(True)
                    | (ApprovalExecution.lease_until > now)
                ),
            )
            .limit(1)
        ):
            raise Conflict("审批恢复产物必须保留")
        return session

    def archive(self, sid, *, dry_run=True):
        with self.factory() as db:
            approval_ids = list(
                db.scalars(
                    select(ApprovalRequest.id)
                    .where(ApprovalRequest.session_id == sid)
                    .order_by(ApprovalRequest.id)
                )
            )
        tids = [thread_id(sid), *(approval_thread(a) for a in approval_ids)]
        with ExitStack() as stack:
            savers = [
                stack.enter_context(locked_saver(self.postgres_url, tid, instance="retention"))[0]
                for tid in tids
            ]
            with self.factory() as db, db.begin():
                session = self.check(db, sid)
                from app.graphs.approval.graph import build_approval_graph
                from app.graphs.negotiation.graph import build_graph
                from app.graphs.negotiation.semantic_graph import build_semantic_graph
                from app.runtime.input_queue import InputQueue
                from app.services.graph_approval_service import GraphApprovalService

                if session.graph_version not in {
                    "negotiation-v1",
                    "negotiation-v2",
                    "negotiation-v3",
                }:
                    raise Conflict("未知图版本必须由原版本完成保留核验")
                for saver, tid in zip(savers, tids, strict=True):
                    if tid.startswith("approval:"):
                        graph = build_approval_graph(
                            saver,
                            GraphApprovalService(self.factory),
                            None,
                            lambda _: None,
                            lambda _: None,
                        )
                    elif session.graph_version == "negotiation-v1":
                        graph = build_graph(saver, InputQueue(self.factory), None)
                    else:
                        graph = build_semantic_graph(
                            saver,
                            InputQueue(self.factory),
                            None,
                            quality=session.graph_version == "negotiation-v3",
                        )
                    if graph.get_state({"configurable": {"thread_id": tid}}).next:
                        raise Conflict("仍有待执行 checkpoint，不能清理")
                if not dry_run:
                    session.archived_at = session.archived_at or self.clock()
            if not dry_run:
                # A crash here leaves an archive tombstone and retained checkpoints; rerun is safe.
                for saver, tid in zip(savers, tids, strict=True):
                    saver.delete_thread(tid)
            return {
                "session_id": sid,
                "archived": not dry_run,
                "audit_and_idempotency_retained": True,
                "thread_count": len(tids),
            }
