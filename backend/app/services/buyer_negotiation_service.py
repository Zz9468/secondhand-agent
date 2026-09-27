from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.db.models import NegotiationSession, NegotiationStatus, Product, ProductStatus
from app.services.product_service import PublicSellerSummary


@dataclass(frozen=True, slots=True)
class BuyerNegotiationSummary:
    id: int
    product_id: int
    product_title: str
    product_status: ProductStatus
    seller: PublicSellerSummary
    status: NegotiationStatus
    current_offer_id: int | None
    confirmed_offer_id: int | None
    round_count: int
    created_at: datetime
    updated_at: datetime


class BuyerNegotiationService:
    """列出当前账号作为买家的协商历史，不暴露其他买家会话。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_for_buyer(
        self,
        *,
        buyer_id: str,
        limit: int = 200,
    ) -> tuple[BuyerNegotiationSummary, ...]:
        with self._session_factory() as db:
            negotiations = db.scalars(
                select(NegotiationSession)
                .where(NegotiationSession.buyer_id == buyer_id)
                .options(
                    selectinload(NegotiationSession.product).selectinload(
                        Product.seller
                    )
                )
                .order_by(
                    NegotiationSession.updated_at.desc(),
                    NegotiationSession.id.desc(),
                )
                .limit(limit)
            )
            return tuple(self._summary(item) for item in negotiations)

    @staticmethod
    def _summary(negotiation: NegotiationSession) -> BuyerNegotiationSummary:
        product = negotiation.product
        return BuyerNegotiationSummary(
            id=negotiation.id,
            product_id=product.id,
            product_title=product.title,
            product_status=product.status,
            seller=PublicSellerSummary(
                id=product.seller.id,
                display_name=product.seller.display_name,
            ),
            status=negotiation.status,
            current_offer_id=negotiation.current_offer_id,
            confirmed_offer_id=negotiation.confirmed_offer_id,
            round_count=negotiation.round_count,
            created_at=negotiation.created_at,
            updated_at=negotiation.updated_at,
        )
