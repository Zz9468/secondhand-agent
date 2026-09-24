from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.enums import NegotiationStyle, stored_enum

if TYPE_CHECKING:
    from app.db.models.product import Product


class SellerPolicy(TimestampMixin, Base):
    """卖家为单个商品设置的私有协商规则。"""

    __tablename__ = "seller_policies"
    __table_args__ = (
        UniqueConstraint("product_id", name="uq_seller_policies_product"),
        CheckConstraint("minimum_net_price >= 0", name="minimum_net_price_nonnegative"),
        CheckConstraint(
            "auto_accept_threshold >= minimum_net_price",
            name="threshold_not_below_minimum",
        ),
        CheckConstraint("max_rounds > 0", name="max_rounds_positive"),
        CheckConstraint("version > 0", name="version_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
    )
    minimum_net_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    auto_accept_threshold: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    negotiation_style: Mapped[NegotiationStyle] = mapped_column(
        stored_enum(NegotiationStyle, name="negotiation_style"),
        nullable=False,
        default=NegotiationStyle.BALANCED,
    )
    max_rounds: Mapped[int] = mapped_column(nullable=False, default=6)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    product: Mapped["Product"] = relationship(back_populates="policy")
