import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.decision_provider import ConversationMessage, DecisionProvider
from app.agent.seller_agent import AgentTurnOutcome, SellerAgent
from app.agent.tools import AgentToolContext, build_seller_tools
from app.db.models import Message, MessageRole, NegotiationSession, NegotiationStatus
from app.services.approval_service import ApprovalService
from app.services.errors import (
    IncompleteRequestError,
    MessageConflictError,
    ModelDecisionError,
    NegotiationNotFoundError,
)
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import OfferTerms, ShippingPayer
from app.services.product_service import ProductService


@dataclass(frozen=True, slots=True)
class BuyerOfferSubmission:
    price: Decimal
    shipping_paid_by: ShippingPayer
    shipping_cost: Decimal | None = None
    delivery_method: str | None = None

    def to_terms(self) -> OfferTerms:
        return OfferTerms(
            buyer_payment=self.price,
            shipping_paid_by=self.shipping_paid_by,
            shipping_cost=self.shipping_cost,
        )

    def additional_terms(self) -> dict[str, object]:
        if self.delivery_method is None:
            return {}
        return {"delivery_method": self.delivery_method}


@dataclass(frozen=True, slots=True)
class MessageSnapshot:
    id: int
    role: MessageRole
    content: str
    request_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChatTurnSnapshot:
    buyer_message: MessageSnapshot
    agent_message: MessageSnapshot
    outcome: str
    formal_offer_id: int | None
    idempotent_replay: bool


class ChatService:
    """持久化聊天消息，并把当前请求安全地交给 Seller Agent。"""

    _history_limit = 20

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        decision_provider: DecisionProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._decision_provider = decision_provider
        self._negotiation_service = NegotiationService(session_factory)
        self._approval_service = ApprovalService(session_factory)

    def send_buyer_message(
        self,
        *,
        session_id: int,
        buyer_id: str,
        request_id: str,
        content: str,
        offer: BuyerOfferSubmission | None = None,
    ) -> ChatTurnSnapshot:
        reply_request_id = self._reply_request_id(request_id)
        request_fingerprint = self._request_fingerprint(content=content, offer=offer)
        with self._session_factory() as db, db.begin():
            negotiation = self._require_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
                for_update=True,
            )
            existing_buyer = self._message_by_request_id(
                db,
                session_id=session_id,
                request_id=request_id,
            )
            if existing_buyer is not None:
                return self._replay_existing(
                    db,
                    existing_buyer=existing_buyer,
                    reply_request_id=reply_request_id,
                    expected_fingerprint=request_fingerprint,
                )
            if negotiation.status in {
                NegotiationStatus.AGREED,
                NegotiationStatus.CLOSED,
            }:
                raise MessageConflictError("当前会话已经结束，不能继续发送消息")

            history = self._recent_history(db, session_id=session_id)
            buyer_offer_id: int | None = None
            if offer is not None:
                if negotiation.status is NegotiationStatus.WAITING_APPROVAL:
                    # 新正式报价会明确撤销旧审批；普通聊天不会改变审批状态。
                    self._approval_service.cancel_pending_for_new_offer_in_transaction(
                        db=db,
                        session_id=session_id,
                        buyer_id=buyer_id,
                    )
                buyer_offer = self._negotiation_service.record_buyer_offer_in_transaction(
                    db=db,
                    session_id=session_id,
                    buyer_id=buyer_id,
                    terms=offer.to_terms(),
                    additional_terms=offer.additional_terms(),
                )
                buyer_offer_id = buyer_offer.id

            buyer_message = Message(
                session_id=session_id,
                role=MessageRole.BUYER,
                content=content,
                request_id=request_id,
                request_fingerprint=request_fingerprint,
            )
            db.add(buyer_message)
            db.flush()
            db.refresh(buyer_message)
            buyer_snapshot = self._snapshot(buyer_message)

            # 一轮聊天使用同一连接和外层事务。工具通过 SAVEPOINT 执行，
            # 任何未处理异常都会连同买家消息、报价和 Agent 动作一起回滚。
            turn_session_factory = sessionmaker(
                bind=db.connection(),
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            agent = self._build_agent(
                session_id=session_id,
                buyer_id=buyer_id,
                session_factory=turn_session_factory,
                current_turn_offer_id=buyer_offer_id,
            )
            result = agent.handle_turn(
                content,
                conversation_history=history,
                current_turn_offer_id=buyer_offer_id,
            )
            if result.outcome is AgentTurnOutcome.MODEL_ERROR:
                # 模型失败时不提交本轮消息、报价或旧审批撤销，允许客户端安全重试。
                raise ModelDecisionError("模型暂时无法完成本轮决策，请稍后重试")
            agent_message = Message(
                session_id=session_id,
                role=MessageRole.AGENT,
                content=result.reply,
                request_id=reply_request_id,
                agent_outcome=result.outcome.value,
                formal_offer_id=result.formal_offer_id,
            )
            db.add(agent_message)
            db.flush()
            db.refresh(agent_message)
            agent_snapshot = self._snapshot(agent_message)

        return ChatTurnSnapshot(
            buyer_message=buyer_snapshot,
            agent_message=agent_snapshot,
            outcome=result.outcome.value,
            formal_offer_id=result.formal_offer_id,
            idempotent_replay=False,
        )

    def list_messages(
        self,
        *,
        session_id: int,
        buyer_id: str,
        after_id: int = 0,
        limit: int = 100,
    ) -> tuple[MessageSnapshot, ...]:
        with self._session_factory() as db:
            self._require_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
            )
            messages = db.scalars(
                select(Message)
                .where(
                    Message.session_id == session_id,
                    Message.id > after_id,
                )
                .order_by(Message.id)
                .limit(limit)
            )
            return tuple(self._snapshot(message) for message in messages)

    def _build_agent(
        self,
        *,
        session_id: int,
        buyer_id: str,
        session_factory: sessionmaker[Session] | None = None,
        current_turn_offer_id: int | None = None,
    ) -> SellerAgent:
        if self._decision_provider is None:
            raise RuntimeError("发送消息前必须配置决策模型")
        active_session_factory = session_factory or self._session_factory
        tools = build_seller_tools(
            context=AgentToolContext(
                session_id=session_id,
                buyer_id=buyer_id,
                current_turn_offer_id=current_turn_offer_id,
            ),
            product_service=ProductService(active_session_factory),
            negotiation_service=NegotiationService(active_session_factory),
            approval_service=ApprovalService(active_session_factory),
        )
        return SellerAgent(
            decision_provider=self._decision_provider,
            tools=tools,
        )

    def _replay_existing(
        self,
        db: Session,
        *,
        existing_buyer: Message,
        reply_request_id: str,
        expected_fingerprint: str,
    ) -> ChatTurnSnapshot:
        if existing_buyer.role is not MessageRole.BUYER:
            raise MessageConflictError("请求幂等键已被其他消息占用")
        if existing_buyer.request_fingerprint != expected_fingerprint:
            raise MessageConflictError("相同请求幂等键不能用于不同请求内容或报价")
        existing_reply = self._message_by_request_id(
            db,
            session_id=existing_buyer.session_id,
            request_id=reply_request_id,
        )
        if existing_reply is None:
            # 新版本会原子提交整轮；这里只保护升级前可能遗留的不完整消息。
            raise IncompleteRequestError("该请求尚未形成完整回复，请稍后查询消息记录")
        if existing_reply.role is not MessageRole.AGENT:
            raise MessageConflictError("回复幂等键已被其他消息占用")
        return ChatTurnSnapshot(
            buyer_message=self._snapshot(existing_buyer),
            agent_message=self._snapshot(existing_reply),
            outcome=existing_reply.agent_outcome or "IDEMPOTENT_REPLAY",
            formal_offer_id=existing_reply.formal_offer_id,
            idempotent_replay=True,
        )

    @staticmethod
    def _require_negotiation(
        db: Session,
        *,
        session_id: int,
        buyer_id: str,
        for_update: bool = False,
    ) -> NegotiationSession:
        statement = select(NegotiationSession).where(
            NegotiationSession.id == session_id,
            NegotiationSession.buyer_id == buyer_id,
        )
        if for_update:
            statement = statement.with_for_update()
        negotiation = db.scalar(statement)
        if negotiation is None:
            raise NegotiationNotFoundError("协商会话不存在或当前买家无权访问")
        return negotiation

    def _recent_history(
        self,
        db: Session,
        *,
        session_id: int,
    ) -> tuple[ConversationMessage, ...]:
        items = list(
            db.scalars(
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(Message.id.desc())
                .limit(self._history_limit)
            )
        )
        items.reverse()
        return tuple(
            ConversationMessage(role=item.role.value, content=item.content)
            for item in items
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

    @staticmethod
    def _reply_request_id(request_id: str) -> str:
        return f"{request_id}:agent"

    @staticmethod
    def _request_fingerprint(
        *,
        content: str,
        offer: BuyerOfferSubmission | None,
    ) -> str:
        offer_payload = None
        if offer is not None:
            offer_payload = {
                "price": format(offer.price, ".2f"),
                "shipping_paid_by": offer.shipping_paid_by.value,
                "shipping_cost": (
                    format(offer.shipping_cost, ".2f")
                    if offer.shipping_cost is not None
                    else None
                ),
                "delivery_method": offer.delivery_method,
            }
        canonical = json.dumps(
            {"content": content, "offer": offer_payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _snapshot(message: Message) -> MessageSnapshot:
        return MessageSnapshot(
            id=message.id,
            role=message.role,
            content=message.content,
            request_id=message.request_id,
            created_at=message.created_at,
        )
