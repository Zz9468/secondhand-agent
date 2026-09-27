from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.db.models import (
    ApprovalFollowupStatus,
    ApprovalRequest,
    ApprovalStatus,
    ConfirmationSource,
    Message,
    MessageRole,
    NegotiationSession,
    NegotiationStatus,
    Product,
    ProductStatus,
)
from app.services.approval_service import render_seller_approval_reason
from app.services.errors import NegotiationNotFoundError
from app.services.negotiation_service import NegotiationService, OfferSnapshot


@dataclass(frozen=True, slots=True)
class SellerNegotiationApprovalSnapshot:
    id: int
    offer_id: int
    policy_version: int
    status: ApprovalStatus
    reason: str
    seller_comment: str | None
    expires_at: datetime
    reviewed_at: datetime | None
    followup_status: ApprovalFollowupStatus | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SellerNegotiationMessageSnapshot:
    id: int
    role: MessageRole
    content: str
    formal_offer_id: int | None
    agent_outcome: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SellerNegotiationSummary:
    id: int
    product_id: int
    product_title: str
    product_status: ProductStatus
    buyer_display_name: str
    status: NegotiationStatus
    current_offer_id: int | None
    confirmed_offer_id: int | None
    confirmed_at: datetime | None
    confirmation_source: ConfirmationSource | None
    round_count: int
    version: int
    current_offer: OfferSnapshot | None
    confirmed_offer: OfferSnapshot | None
    latest_approval: SellerNegotiationApprovalSnapshot | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SellerNegotiationDetail:
    negotiation: SellerNegotiationSummary
    messages: tuple[SellerNegotiationMessageSnapshot, ...]
    offers: tuple[OfferSnapshot, ...]
    approvals: tuple[SellerNegotiationApprovalSnapshot, ...]


class SellerNegotiationService:
    """提供卖家只读会话视图，并在查询层校验商品所有权。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_for_seller(
        self,
        *,
        seller_id: str,
        negotiation_status: NegotiationStatus | None = None,
        limit: int = 200,
    ) -> tuple[SellerNegotiationSummary, ...]:
        with self._session_factory() as db:
            statement = (
                select(NegotiationSession)
                .join(Product, Product.id == NegotiationSession.product_id)
                .where(Product.seller_id == seller_id)
                .options(
                    selectinload(NegotiationSession.product),
                    selectinload(NegotiationSession.buyer),
                    selectinload(NegotiationSession.current_offer),
                    selectinload(NegotiationSession.confirmed_offer),
                    selectinload(NegotiationSession.approval_requests).selectinload(
                        ApprovalRequest.offer
                    ),
                )
                .order_by(
                    NegotiationSession.updated_at.desc(),
                    NegotiationSession.id.desc(),
                )
                .limit(limit)
            )
            if negotiation_status is not None:
                statement = statement.where(
                    NegotiationSession.status == negotiation_status
                )
            negotiations = tuple(db.scalars(statement).unique())
            return tuple(self._summary(item) for item in negotiations)

    def get_for_seller(
        self,
        *,
        seller_id: str,
        session_id: int,
    ) -> SellerNegotiationDetail:
        with self._session_factory() as db:
            negotiation = db.scalar(
                select(NegotiationSession)
                .join(Product, Product.id == NegotiationSession.product_id)
                .where(
                    NegotiationSession.id == session_id,
                    Product.seller_id == seller_id,
                )
                .options(
                    selectinload(NegotiationSession.product),
                    selectinload(NegotiationSession.buyer),
                    selectinload(NegotiationSession.current_offer),
                    selectinload(NegotiationSession.confirmed_offer),
                    selectinload(NegotiationSession.messages),
                    selectinload(NegotiationSession.offers),
                    selectinload(NegotiationSession.approval_requests).selectinload(
                        ApprovalRequest.offer
                    ),
                )
            )
            if negotiation is None:
                # 资源不存在和不属于当前卖家统一返回不存在，避免泄露其他卖家数据。
                raise NegotiationNotFoundError("协商会话不存在或当前卖家无权访问")
            return SellerNegotiationDetail(
                negotiation=self._summary(negotiation),
                messages=tuple(
                    self._message_snapshot(message) for message in negotiation.messages
                ),
                offers=tuple(
                    NegotiationService._snapshot(offer) for offer in negotiation.offers
                ),
                approvals=tuple(
                    self._approval_snapshot(approval)
                    for approval in negotiation.approval_requests
                ),
            )

    @classmethod
    def _summary(cls, negotiation: NegotiationSession) -> SellerNegotiationSummary:
        latest_approval = (
            cls._approval_snapshot(negotiation.approval_requests[-1])
            if negotiation.approval_requests
            else None
        )
        return SellerNegotiationSummary(
            id=negotiation.id,
            product_id=negotiation.product_id,
            product_title=negotiation.product.title,
            product_status=negotiation.product.status,
            buyer_display_name=negotiation.buyer.display_name,
            status=negotiation.status,
            current_offer_id=negotiation.current_offer_id,
            confirmed_offer_id=negotiation.confirmed_offer_id,
            confirmed_at=negotiation.confirmed_at,
            confirmation_source=negotiation.confirmation_source,
            round_count=negotiation.round_count,
            version=negotiation.version,
            current_offer=(
                NegotiationService._snapshot(negotiation.current_offer)
                if negotiation.current_offer is not None
                else None
            ),
            confirmed_offer=(
                NegotiationService._snapshot(negotiation.confirmed_offer)
                if negotiation.confirmed_offer is not None
                else None
            ),
            latest_approval=latest_approval,
            created_at=negotiation.created_at,
            updated_at=negotiation.updated_at,
        )

    @staticmethod
    def _approval_snapshot(
        approval: ApprovalRequest,
    ) -> SellerNegotiationApprovalSnapshot:
        return SellerNegotiationApprovalSnapshot(
            id=approval.id,
            offer_id=approval.offer_id,
            policy_version=approval.policy_version,
            status=approval.status,
            reason=render_seller_approval_reason(approval.offer),
            seller_comment=approval.seller_comment,
            expires_at=approval.expires_at,
            reviewed_at=approval.reviewed_at,
            followup_status=approval.followup_status,
            created_at=approval.created_at,
            updated_at=approval.updated_at,
        )

    @staticmethod
    def _message_snapshot(message: Message) -> SellerNegotiationMessageSnapshot:
        return SellerNegotiationMessageSnapshot(
            id=message.id,
            role=message.role,
            content=message.content,
            formal_offer_id=message.formal_offer_id,
            agent_outcome=message.agent_outcome,
            created_at=message.created_at,
        )
