from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.enums import NegotiationStatus, stored_enum

if TYPE_CHECKING:
    from app.db.models.message import Message
    from app.db.models.offer import Offer
    from app.db.models.product import Product


class NegotiationSession(TimestampMixin, Base):
    """买家针对某件商品建立的持久化协商会话。"""

    __tablename__ = "negotiation_sessions"
    __table_args__ = (
        CheckConstraint("round_count >= 0", name="round_count_nonnegative"),
        CheckConstraint("version > 0", name="version_positive"),
        Index("ix_negotiation_sessions_product_buyer", "product_id", "buyer_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    buyer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[NegotiationStatus] = mapped_column(
        stored_enum(NegotiationStatus, name="negotiation_status"),
        nullable=False,
        default=NegotiationStatus.ACTIVE,
    )
    current_offer_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "offers.id",
            name="fk_negotiation_sessions_current_offer_id_offers",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
    )
    round_count: Mapped[int] = mapped_column(nullable=False, default=0)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    product: Mapped["Product"] = relationship(back_populates="negotiations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Message.id",
    )
    offers: Mapped[list["Offer"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        foreign_keys="Offer.session_id",
        order_by="Offer.id",
    )
    current_offer: Mapped["Offer | None"] = relationship(
        foreign_keys=[current_offer_id],
        post_update=True,
    )
