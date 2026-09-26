import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    ApprovalFollowupStatus,
    ApprovalRequest,
    ApprovalStatus,
    ConfirmationSource,
    Message,
    MessageRole,
    NegotiationSession,
    NegotiationStatus,
    Offer,
    OfferProposer,
    OfferStatus,
    Product,
    ProductStatus,
    SellerPolicy,
)
from app.services.errors import (
    NegotiationLifecycleConflictError,
    NegotiationNotFoundError,
    ServiceError,
)
from app.services.negotiation_service import NegotiationService

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class LifecycleMessageSnapshot:
    id: int
    role: MessageRole
    content: str
    request_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IntentConfirmationSnapshot:
    session_id: int
    status: NegotiationStatus
    confirmed_offer_id: int
    confirmed_at: datetime
    confirmation_source: ConfirmationSource
    system_message: LifecycleMessageSnapshot
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class NegotiationClosureSnapshot:
    session_id: int
    status: NegotiationStatus
    cancelled_approval_id: int | None
    system_message: LifecycleMessageSnapshot
    idempotent_replay: bool


class IntentService:
    """校验买家最终确认与关闭动作，并持久化可审计的会话终态。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._negotiation_service = NegotiationService(session_factory)

    def confirm_offer(
        self,
        *,
        session_id: int,
        buyer_id: str,
        offer_id: int,
        request_id: str,
    ) -> IntentConfirmationSnapshot:
        """只有当前、有效且具有可证明授权来源的报价才能形成交易意向。"""

        stored_request_id = self._validated_request_id(request_id)
        with self._session_factory() as db, db.begin():
            negotiation = self._get_owned_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
                for_update=True,
            )
            if negotiation.status is NegotiationStatus.AGREED:
                return self._replay_confirmation(
                    db,
                    negotiation=negotiation,
                    offer_id=offer_id,
                )
            if negotiation.status is not NegotiationStatus.ACTIVE:
                raise NegotiationLifecycleConflictError(
                    "当前会话不能确认报价，请等待审批完成或检查会话状态"
                )
            if negotiation.current_offer_id != offer_id:
                raise NegotiationLifecycleConflictError("只能确认当前有效报价")

            product = db.get(Product, negotiation.product_id, with_for_update=True)
            if product is None or product.status is not ProductStatus.AVAILABLE:
                raise NegotiationLifecycleConflictError("商品当前不可确认交易意向")
            offer = db.scalar(
                select(Offer)
                .where(Offer.id == offer_id, Offer.session_id == negotiation.id)
                .with_for_update()
            )
            if offer is None:
                raise NegotiationLifecycleConflictError("待确认报价不存在")
            if NegotiationService._is_expired(offer.expires_at):
                raise NegotiationLifecycleConflictError("待确认报价已经过期")
            policy = db.scalar(
                select(SellerPolicy)
                .where(SellerPolicy.product_id == negotiation.product_id)
                .with_for_update()
            )
            if policy is None:
                raise NegotiationLifecycleConflictError("商品缺少有效卖家规则")

            source = self._confirmation_source(
                db,
                negotiation=negotiation,
                offer=offer,
                policy=policy,
            )
            system_request_id = self._system_request_id(
                "confirm",
                stored_request_id,
            )
            if self._message_by_request_id(
                db,
                session_id=negotiation.id,
                request_id=system_request_id,
            ) is not None:
                raise NegotiationLifecycleConflictError(
                    "确认幂等键已经被不完整请求占用"
                )

            confirmed_at = datetime.now()
            offer.status = OfferStatus.ACCEPTED
            negotiation.status = NegotiationStatus.AGREED
            negotiation.confirmed_offer_id = offer.id
            negotiation.confirmed_at = confirmed_at
            negotiation.confirmation_request_id = stored_request_id
            negotiation.confirmation_source = source
            negotiation.version += 1
            message = Message(
                session_id=negotiation.id,
                role=MessageRole.SYSTEM,
                content=self._confirmation_message(offer),
                request_id=system_request_id,
                formal_offer_id=offer.id,
            )
            db.add(message)
            try:
                db.flush()
            except IntegrityError as exc:
                raise NegotiationLifecycleConflictError(
                    "确认幂等键已经被其他请求使用"
                ) from exc
            db.refresh(message)
            return IntentConfirmationSnapshot(
                session_id=negotiation.id,
                status=negotiation.status,
                confirmed_offer_id=offer.id,
                confirmed_at=confirmed_at,
                confirmation_source=source,
                system_message=self._message_snapshot(message),
                idempotent_replay=False,
            )

    def close_negotiation(
        self,
        *,
        session_id: int,
        buyer_id: str,
        request_id: str,
    ) -> NegotiationClosureSnapshot:
        """结束未达成意向的会话，并在同一事务内取消待审批请求。"""

        stored_request_id = self._validated_request_id(request_id)
        with self._session_factory() as db, db.begin():
            negotiation = self._get_owned_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
                for_update=True,
            )
            if negotiation.status is NegotiationStatus.AGREED:
                raise NegotiationLifecycleConflictError(
                    "已经形成交易意向的会话不能改为关闭"
                )
            if negotiation.status is NegotiationStatus.CLOSED:
                return self._replay_closure(db, negotiation=negotiation)
            if negotiation.status not in {
                NegotiationStatus.ACTIVE,
                NegotiationStatus.WAITING_APPROVAL,
            }:
                raise NegotiationLifecycleConflictError("当前会话不能关闭")

            pending = db.scalar(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.session_id == negotiation.id,
                    ApprovalRequest.status == ApprovalStatus.PENDING,
                )
                .with_for_update()
            )
            cancelled_approval_id: int | None = None
            if pending is not None:
                pending.status = ApprovalStatus.CANCELLED
                cancelled_approval_id = pending.id

            system_request_id = self._system_request_id("close", stored_request_id)
            if self._message_by_request_id(
                db,
                session_id=negotiation.id,
                request_id=system_request_id,
            ) is not None:
                raise NegotiationLifecycleConflictError(
                    "关闭幂等键已经被不完整请求占用"
                )
            negotiation.status = NegotiationStatus.CLOSED
            negotiation.version += 1
            message = Message(
                session_id=negotiation.id,
                role=MessageRole.SYSTEM,
                content="买家已结束本次协商；系统未记录交易意向或实际成交。",
                request_id=system_request_id,
                agent_outcome="NEGOTIATION_CLOSED",
            )
            db.add(message)
            db.flush()
            db.refresh(message)
            return NegotiationClosureSnapshot(
                session_id=negotiation.id,
                status=negotiation.status,
                cancelled_approval_id=cancelled_approval_id,
                system_message=self._message_snapshot(message),
                idempotent_replay=False,
            )

    def _confirmation_source(
        self,
        db: Session,
        *,
        negotiation: NegotiationSession,
        offer: Offer,
        policy: SellerPolicy,
    ) -> ConfirmationSource:
        try:
            authorization = self._negotiation_service.authorize_stored_offer(
                offer=offer,
                policy=policy,
            )
        except ServiceError as exc:
            raise NegotiationLifecycleConflictError(
                "报价交易条件无法重新授权"
            ) from exc

        if offer.proposer is OfferProposer.AGENT:
            if (
                offer.status is not OfferStatus.PROPOSED
                or not authorization.can_submit_counter_offer
                or not self._has_formal_agent_message(
                    db,
                    session_id=negotiation.id,
                    offer_id=offer.id,
                    outcome="COUNTER_OFFERED",
                )
            ):
                raise NegotiationLifecycleConflictError(
                    "当前 Agent 报价缺少有效授权或正式回复记录"
                )
            return ConfirmationSource.AGENT_COUNTER

        if offer.proposer is not OfferProposer.BUYER:
            raise NegotiationLifecycleConflictError("报价提出方无效")
        if offer.status is not OfferStatus.ACCEPTED:
            raise NegotiationLifecycleConflictError("买家报价尚未获得接受授权")

        approval = db.scalar(
            select(ApprovalRequest)
            .where(ApprovalRequest.offer_id == offer.id)
            .with_for_update()
        )
        if approval is not None:
            if (
                approval.status is not ApprovalStatus.APPROVED
                or approval.followup_status is not ApprovalFollowupStatus.SENT
                or approval.policy_version != policy.version
                or not authorization.can_request_approval
                or not self._has_formal_agent_message(
                    db,
                    session_id=negotiation.id,
                    offer_id=offer.id,
                    outcome="APPROVAL_APPROVED",
                )
            ):
                raise NegotiationLifecycleConflictError(
                    "卖家审批授权已经失效或尚未完成通知"
                )
            return ConfirmationSource.SELLER_APPROVED_BUYER_OFFER

        if (
            not authorization.can_accept_automatically
            or not self._has_formal_agent_message(
                db,
                session_id=negotiation.id,
                offer_id=offer.id,
                outcome="OFFER_ACCEPTED",
            )
        ):
            raise NegotiationLifecycleConflictError(
                "买家报价缺少有效的自动接受记录"
            )
        return ConfirmationSource.AUTO_ACCEPTED_BUYER_OFFER

    @staticmethod
    def _get_owned_negotiation(
        db: Session,
        *,
        session_id: int,
        buyer_id: str,
        for_update: bool,
    ) -> NegotiationSession:
        statement = select(NegotiationSession).where(
            NegotiationSession.id == session_id,
            NegotiationSession.buyer_id == buyer_id,
        )
        if for_update:
            statement = statement.with_for_update()
        negotiation = db.scalar(statement)
        if negotiation is None:
            raise NegotiationNotFoundError(
                "协商会话不存在或当前买家无权访问"
            )
        return negotiation

    @staticmethod
    def _has_formal_agent_message(
        db: Session,
        *,
        session_id: int,
        offer_id: int,
        outcome: str,
    ) -> bool:
        return (
            db.scalar(
                select(Message.id).where(
                    Message.session_id == session_id,
                    Message.role == MessageRole.AGENT,
                    Message.formal_offer_id == offer_id,
                    Message.agent_outcome == outcome,
                )
            )
            is not None
        )

    @staticmethod
    def _message_by_request_id(
        db: Session,
        *,
        session_id: int,
        request_id: str,
    ) -> Message | None:
        return db.scalar(
            select(Message).where(
                Message.session_id == session_id,
                Message.request_id == request_id,
            )
        )

    def _replay_confirmation(
        self,
        db: Session,
        *,
        negotiation: NegotiationSession,
        offer_id: int,
    ) -> IntentConfirmationSnapshot:
        if (
            negotiation.confirmed_offer_id != offer_id
            or negotiation.confirmed_at is None
            or negotiation.confirmation_request_id is None
            or negotiation.confirmation_source is None
        ):
            raise NegotiationLifecycleConflictError(
                "会话已经形成其他报价的交易意向"
            )
        message = self._message_by_request_id(
            db,
            session_id=negotiation.id,
            request_id=self._system_request_id(
                "confirm",
                negotiation.confirmation_request_id,
            ),
        )
        if message is None or message.role is not MessageRole.SYSTEM:
            raise NegotiationLifecycleConflictError("确认记录缺少对应系统消息")
        return IntentConfirmationSnapshot(
            session_id=negotiation.id,
            status=negotiation.status,
            confirmed_offer_id=offer_id,
            confirmed_at=negotiation.confirmed_at,
            confirmation_source=negotiation.confirmation_source,
            system_message=self._message_snapshot(message),
            idempotent_replay=True,
        )

    def _replay_closure(
        self,
        db: Session,
        *,
        negotiation: NegotiationSession,
    ) -> NegotiationClosureSnapshot:
        message = db.scalar(
            select(Message)
            .where(
                Message.session_id == negotiation.id,
                Message.role == MessageRole.SYSTEM,
                Message.agent_outcome == "NEGOTIATION_CLOSED",
            )
            .order_by(Message.id)
            .limit(1)
        )
        if message is None:
            raise NegotiationLifecycleConflictError("关闭会话缺少对应系统消息")
        return NegotiationClosureSnapshot(
            session_id=negotiation.id,
            status=negotiation.status,
            cancelled_approval_id=None,
            system_message=self._message_snapshot(message),
            idempotent_replay=True,
        )

    @staticmethod
    def _confirmation_message(offer: Offer) -> str:
        shipping = (
            "包邮（运费由卖家承担）"
            if offer.shipping_paid_by.value == "seller"
            else "不包邮（运费由买家承担）"
        )
        discount = (
            f"，另含卖家承担优惠 {format(offer.seller_borne_discount, '.2f')} 元"
            if offer.seller_borne_discount
            else ""
        )
        delivery = offer.terms.get("delivery_method")
        delivery_text = (
            "，交易方式为面交"
            if delivery == "pickup"
            else "，交易方式为快递" if delivery == "shipping" else ""
        )
        return (
            f"买家已明确确认报价 #{offer.id}：{format(offer.price, '.2f')} 元，"
            f"{shipping}{discount}{delivery_text}。"
            "系统已记录双方对上述条件的交易意向；"
            "这不代表实际成交、付款、库存锁定或履约完成。"
        )

    @staticmethod
    def _message_snapshot(message: Message) -> LifecycleMessageSnapshot:
        return LifecycleMessageSnapshot(
            id=message.id,
            role=message.role,
            content=message.content,
            request_id=message.request_id,
            created_at=message.created_at,
        )

    @staticmethod
    def _validated_request_id(request_id: str) -> str:
        stored = request_id.strip()
        if (
            not 8 <= len(stored) <= 56
            or _REQUEST_ID_PATTERN.fullmatch(stored) is None
        ):
            raise NegotiationLifecycleConflictError("请求幂等键格式不正确")
        return stored

    @staticmethod
    def _system_request_id(prefix: str, request_id: str) -> str:
        return f"{prefix}:{request_id}"
