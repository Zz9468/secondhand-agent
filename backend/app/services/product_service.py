from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import NegotiationSession, Product, ProductStatus
from app.services.errors import NegotiationNotFoundError


@dataclass(frozen=True, slots=True)
class ProductInfo:
    id: int
    title: str
    description: str
    listed_price: Decimal
    status: ProductStatus


class ProductService:
    """只通过已绑定会话返回可向买家公开的商品事实。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_for_negotiation(self, *, session_id: int, buyer_id: str) -> ProductInfo:
        with self._session_factory() as db:
            product = db.scalar(
                select(Product)
                .join(
                    NegotiationSession,
                    NegotiationSession.product_id == Product.id,
                )
                .where(
                    NegotiationSession.id == session_id,
                    NegotiationSession.buyer_id == buyer_id,
                )
            )
            if product is None:
                raise NegotiationNotFoundError("协商会话不存在或当前买家无权访问")

            return ProductInfo(
                id=product.id,
                title=product.title,
                description=product.description,
                listed_price=product.listed_price,
                status=product.status,
            )
