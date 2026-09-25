from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import (
    OfferProposer,
    OfferStatus,
    ShippingPayer,
    stored_enum,
)

if TYPE_CHECKING:
    from app.db.models.approval import ApprovalRequest
    from app.db.models.negotiation import NegotiationSession


class Offer(Base):
    """不可原地修改交易条件的正式报价快照。"""

    __tablename__ = "offers"
    __table_args__ = (
        CheckConstraint("price >= 0", name="price_nonnegative"),
        CheckConstraint(
            "shipping_cost IS NULL OR shipping_cost >= 0",
            name="shipping_cost_nonnegative",
        ),
        CheckConstraint(
            "seller_borne_discount >= 0",
            name="seller_borne_discount_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("negotiation_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    proposer: Mapped[OfferProposer] = mapped_column(
        stored_enum(OfferProposer, name="offer_proposer"),
        nullable=False,
    )
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    shipping_paid_by: Mapped[ShippingPayer] = mapped_column(
        stored_enum(ShippingPayer, name="shipping_payer"),
        nullable=False,
    )
    shipping_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    seller_borne_discount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0.00"),
    )
    # JSON 仅保存附加条件；价格和成本使用独立 DECIMAL 字段参与授权判断。
    terms: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[OfferStatus] = mapped_column(
        stored_enum(OfferStatus, name="offer_status"),
        nullable=False,
        default=OfferStatus.PROPOSED,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )

    session: Mapped["NegotiationSession"] = relationship(
        back_populates="offers",
        foreign_keys=[session_id],
    )
    approval_requests: Mapped[list["ApprovalRequest"]] = relationship(
        back_populates="offer",
        passive_deletes=True,
    )
