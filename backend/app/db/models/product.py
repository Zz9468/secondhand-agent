from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.enums import ProductStatus, stored_enum

if TYPE_CHECKING:
    from app.db.models.negotiation import NegotiationSession
    from app.db.models.policy import SellerPolicy
    from app.db.models.seller import SellerAccount


class Product(TimestampMixin, Base):
    """买家可见的商品事实信息。"""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("listed_price >= 0", name="listed_price_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    seller_id: Mapped[str] = mapped_column(
        ForeignKey("seller_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    listed_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[ProductStatus] = mapped_column(
        stored_enum(ProductStatus, name="product_status"),
        nullable=False,
        default=ProductStatus.DRAFT,
    )

    policy: Mapped["SellerPolicy | None"] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        single_parent=True,
    )
    negotiations: Mapped[list["NegotiationSession"]] = relationship(
        back_populates="product"
    )
    seller: Mapped["SellerAccount"] = relationship(back_populates="products")
