from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.negotiation import NegotiationSession
    from app.db.models.product import Product


class UserAccount(TimestampMixin, Base):
    """统一用户账号；同一账号可拥有商品，也可作为其他商品的买家。"""

    __tablename__ = "user_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    products: Mapped[list["Product"]] = relationship(back_populates="seller")
    negotiations: Mapped[list["NegotiationSession"]] = relationship(
        back_populates="buyer"
    )
