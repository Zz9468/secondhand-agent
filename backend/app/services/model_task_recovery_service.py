from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    ApprovalFollowupStatus,
    ApprovalRequest,
    Message,
    MessageRole,
    ModelExecutionTask,
    ModelTaskErrorCategory,
    ModelTaskStatus,
    ModelTaskType,
    NegotiationSession,
    Product,
    UserAccount,
)
from app.services.errors import (
    ModelExecutionTaskConflictError,
    ModelExecutionTaskNotFoundError,
)


@dataclass(frozen=True, slots=True)
class SellerModelTaskSnapshot:
    id: int
    task_type: ModelTaskType
    status: ModelTaskStatus
    session_id: int
    product_id: int
    product_title: str
    buyer_display_name: str
    offer_id: int | None
    approval_id: int | None
    attempt_count: int
    max_attempts: int
    manual_retry_count: int
    next_retry_at: datetime | None
    lease_expires_at: datetime | None
    last_error_category: ModelTaskErrorCategory | None
    last_error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    last_manual_action: str | None
    last_manual_actor_id: str | None
    last_manual_reason: str | None
    last_manual_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ModelTaskRecoveryService:
    """向商品卖家提供最小化任务视图和受控人工恢复动作。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_for_seller(
        self,
        *,
        seller_id: str,
        statuses: tuple[ModelTaskStatus, ...] | None = None,
        limit: int = 200,
    ) -> tuple[SellerModelTaskSnapshot, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("模型任务查询数量必须在 1 到 500 之间")
        with self._session_factory() as db:
            statement = (
                self._seller_task_statement(seller_id=seller_id)
                .order_by(ModelExecutionTask.updated_at.desc(), ModelExecutionTask.id.desc())
                .limit(limit)
            )
            if statuses is not None:
                if not statuses:
                    return ()
                statement = statement.where(ModelExecutionTask.status.in_(statuses))
            return tuple(self._snapshot(*row) for row in db.execute(statement))

    def retry_failed(
        self,
        *,
        seller_id: str,
        task_id: int,
        reason: str | None = None,
    ) -> SellerModelTaskSnapshot:
        stored_reason = self._normalized_reason(reason)
        with self._session_factory() as db, db.begin():
            row = self._seller_task_row(
                db,
                seller_id=seller_id,
                task_id=task_id,
                for_update=True,
            )
            task, product_id, product_title, buyer_display_name = row
            if task.status is not ModelTaskStatus.FAILED:
                raise ModelExecutionTaskConflictError("只有终止失败的任务可以人工重试")
            self._require_unfinished_business(db, task=task)

            task.status = ModelTaskStatus.PENDING
            task.max_attempts += 1
            task.manual_retry_count += 1
            task.next_retry_at = None
            task.completed_at = None
            task.lease_owner = None
            task.lease_token = None
            task.lease_expires_at = None
            self._record_manual_action(
                task,
                action="RETRY",
                actor_id=seller_id,
                reason=stored_reason,
            )
            if task.approval_id is not None:
                approval = db.get(ApprovalRequest, task.approval_id, with_for_update=True)
                if approval is not None:
                    approval.followup_status = ApprovalFollowupStatus.FAILED
            db.flush()
            db.refresh(task)
            return self._snapshot(
                task,
                product_id,
                product_title,
                buyer_display_name,
            )

    def terminate(
        self,
        *,
        seller_id: str,
        task_id: int,
        reason: str | None = None,
    ) -> SellerModelTaskSnapshot:
        stored_reason = self._normalized_reason(reason)
        now = datetime.now(UTC).replace(tzinfo=None)
        with self._session_factory() as db, db.begin():
            row = self._seller_task_row(
                db,
                seller_id=seller_id,
                task_id=task_id,
                for_update=True,
            )
            task, product_id, product_title, buyer_display_name = row
            if task.status is ModelTaskStatus.RUNNING and (
                task.lease_expires_at is None or task.lease_expires_at > now
            ):
                raise ModelExecutionTaskConflictError("运行中且租约未过期的任务不能终止")
            if task.status not in {
                ModelTaskStatus.PENDING,
                ModelTaskStatus.RUNNING,
                ModelTaskStatus.RETRY_WAIT,
                ModelTaskStatus.FAILED,
            }:
                raise ModelExecutionTaskConflictError("当前任务状态不允许人工终止")

            task.status = ModelTaskStatus.CANCELLED
            task.next_retry_at = None
            task.lease_owner = None
            task.lease_token = None
            task.lease_expires_at = None
            task.completed_at = now
            self._record_manual_action(
                task,
                action="TERMINATE",
                actor_id=seller_id,
                reason=stored_reason,
                now=now,
            )
            if task.task_type is ModelTaskType.CHAT_DECISION:
                self._persist_chat_termination(db, task=task)
            elif task.approval_id is not None:
                approval = db.get(ApprovalRequest, task.approval_id, with_for_update=True)
                if (
                    approval is not None
                    and approval.followup_status is not ApprovalFollowupStatus.SENT
                ):
                    approval.followup_status = ApprovalFollowupStatus.MANUAL_REQUIRED
            db.flush()
            db.refresh(task)
            return self._snapshot(
                task,
                product_id,
                product_title,
                buyer_display_name,
            )

    @staticmethod
    def _seller_task_statement(*, seller_id: str):
        return (
            select(
                ModelExecutionTask,
                Product.id,
                Product.title,
                UserAccount.display_name,
            )
            .join(
                NegotiationSession,
                NegotiationSession.id == ModelExecutionTask.session_id,
            )
            .join(Product, Product.id == NegotiationSession.product_id)
            .join(UserAccount, UserAccount.id == NegotiationSession.buyer_id)
            .where(Product.seller_id == seller_id)
        )

    def _seller_task_row(
        self,
        db: Session,
        *,
        seller_id: str,
        task_id: int,
        for_update: bool,
    ) -> tuple[ModelExecutionTask, int, str, str]:
        task = db.get(ModelExecutionTask, task_id, with_for_update=for_update)
        if task is None:
            raise ModelExecutionTaskNotFoundError("模型任务不存在或当前卖家无权访问")
        ownership = db.execute(
            select(Product.id, Product.title, UserAccount.display_name)
            .join(
                NegotiationSession,
                NegotiationSession.product_id == Product.id,
            )
            .join(UserAccount, UserAccount.id == NegotiationSession.buyer_id)
            .where(
                NegotiationSession.id == task.session_id,
                Product.seller_id == seller_id,
            )
        ).one_or_none()
        if ownership is None:
            raise ModelExecutionTaskNotFoundError("模型任务不存在或当前卖家无权访问")
        product_id, product_title, buyer_display_name = ownership
        return task, product_id, product_title, buyer_display_name

    @staticmethod
    def _require_unfinished_business(db: Session, *, task: ModelExecutionTask) -> None:
        if task.task_type is ModelTaskType.CHAT_DECISION:
            reply_request_id = task.input_snapshot.get("reply_request_id")
            if isinstance(reply_request_id, str):
                existing = db.scalar(
                    select(Message.id).where(
                        Message.session_id == task.session_id,
                        Message.request_id == reply_request_id,
                    )
                )
                if existing is not None:
                    raise ModelExecutionTaskConflictError("聊天回复已经存在，无需重试")
            return
        approval = db.get(ApprovalRequest, task.approval_id, with_for_update=True)
        if approval is None:
            raise ModelExecutionTaskConflictError("审批记录已经不存在")
        if approval.followup_status is ApprovalFollowupStatus.SENT:
            raise ModelExecutionTaskConflictError("审批通知已经发送，无需重试")

    @staticmethod
    def _persist_chat_termination(db: Session, *, task: ModelExecutionTask) -> None:
        reply_request_id = task.input_snapshot.get("reply_request_id")
        if not isinstance(reply_request_id, str) or not reply_request_id:
            return
        existing = db.scalar(
            select(Message.id).where(
                Message.session_id == task.session_id,
                Message.request_id == reply_request_id,
            )
        )
        if existing is not None:
            return
        db.add(
            Message(
                session_id=task.session_id,
                role=MessageRole.AGENT,
                content="本次请求未能可靠完成，卖家已停止继续处理，请根据当前会话状态重新发送。",
                request_id=reply_request_id,
                agent_outcome="SAFE_FAILURE",
            )
        )

    @staticmethod
    def _record_manual_action(
        task: ModelExecutionTask,
        *,
        action: str,
        actor_id: str,
        reason: str | None,
        now: datetime | None = None,
    ) -> None:
        task.last_manual_action = action
        task.last_manual_actor_id = actor_id
        task.last_manual_reason = reason
        task.last_manual_at = now or datetime.now(UTC).replace(tzinfo=None)

    @staticmethod
    def _normalized_reason(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized[:300] or None

    @staticmethod
    def _snapshot(
        task: ModelExecutionTask,
        product_id: int,
        product_title: str,
        buyer_display_name: str,
    ) -> SellerModelTaskSnapshot:
        return SellerModelTaskSnapshot(
            id=task.id,
            task_type=task.task_type,
            status=task.status,
            session_id=task.session_id,
            product_id=product_id,
            product_title=product_title,
            buyer_display_name=buyer_display_name,
            offer_id=task.offer_id,
            approval_id=task.approval_id,
            attempt_count=task.attempt_count,
            max_attempts=task.max_attempts,
            manual_retry_count=task.manual_retry_count,
            next_retry_at=task.next_retry_at,
            lease_expires_at=task.lease_expires_at,
            last_error_category=task.last_error_category,
            last_error_message=task.last_error_message,
            started_at=task.started_at,
            completed_at=task.completed_at,
            last_manual_action=task.last_manual_action,
            last_manual_actor_id=task.last_manual_actor_id,
            last_manual_reason=task.last_manual_reason,
            last_manual_at=task.last_manual_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
