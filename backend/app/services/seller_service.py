from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Product, ProductStatus, UserAccount
from app.services.errors import SellerNotFoundError


@dataclass(frozen=True, slots=True)
class PublicSellerInfo:
    id: str
    display_name: str
    available_product_count: int


class SellerService:
    """只读取拥有已上架商品的卖家公开资料。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_public(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[PublicSellerInfo, ...]:
        with self._session_factory() as db:
            rows = (
                db.execute(
                    self._public_statement()
                    .order_by(UserAccount.display_name, UserAccount.id)
                    .offset(offset)
                    .limit(limit)
                )
                .mappings()
            )
            return tuple(self._snapshot(row) for row in rows)

    def get_public(self, *, seller_id: str) -> PublicSellerInfo:
        with self._session_factory() as db:
            row = (
                db.execute(
                    self._public_statement().where(UserAccount.id == seller_id)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise SellerNotFoundError("卖家不存在或当前没有已上架商品")
            return self._snapshot(row)

    @staticmethod
    def _public_statement():
        return (
            select(
                UserAccount.id,
                UserAccount.display_name,
                func.count(Product.id).label("available_product_count"),
            )
            .join(Product, Product.seller_id == UserAccount.id)
            .where(Product.status == ProductStatus.AVAILABLE)
            .group_by(UserAccount.id, UserAccount.display_name)
        )

    @staticmethod
    def _snapshot(row: RowMapping) -> PublicSellerInfo:
        return PublicSellerInfo(
            id=row["id"],
            display_name=row["display_name"],
            available_product_count=row["available_product_count"],
        )
