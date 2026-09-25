from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.db.models import (
    NegotiationSession,
    NegotiationStyle,
    Product,
    ProductStatus,
    SellerPolicy,
)
from app.services.errors import (
    NegotiationNotFoundError,
    PolicyVersionConflictError,
    PricingPolicyNotFoundError,
    ProductNotFoundError,
)


@dataclass(frozen=True, slots=True)
class ProductInfo:
    id: int
    title: str
    description: str
    listed_price: Decimal
    status: ProductStatus


@dataclass(frozen=True, slots=True)
class SellerPolicyInfo:
    minimum_net_price: Decimal
    auto_accept_threshold: Decimal
    negotiation_style: NegotiationStyle
    max_rounds: int
    version: int


@dataclass(frozen=True, slots=True)
class SellerProductInfo(ProductInfo):
    policy: SellerPolicyInfo


@dataclass(frozen=True, slots=True)
class ProductCreateData:
    title: str
    description: str
    listed_price: Decimal
    status: ProductStatus
    minimum_net_price: Decimal
    auto_accept_threshold: Decimal
    negotiation_style: NegotiationStyle
    max_rounds: int


@dataclass(frozen=True, slots=True)
class ProductUpdateData:
    title: str
    description: str
    listed_price: Decimal
    status: ProductStatus


@dataclass(frozen=True, slots=True)
class PolicyUpdateData:
    minimum_net_price: Decimal
    auto_accept_threshold: Decimal
    negotiation_style: NegotiationStyle
    max_rounds: int
    expected_version: int


class ProductService:
    """隔离买家公开商品信息与卖家私有规则，并校验商品所有权。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_public(self, *, offset: int = 0, limit: int = 50) -> tuple[ProductInfo, ...]:
        with self._session_factory() as db:
            products = db.scalars(
                select(Product)
                .where(Product.status == ProductStatus.AVAILABLE)
                .order_by(Product.id)
                .offset(offset)
                .limit(limit)
            )
            return tuple(self._public_snapshot(product) for product in products)

    def get_public(self, *, product_id: int) -> ProductInfo:
        with self._session_factory() as db:
            product = db.scalar(
                select(Product).where(
                    Product.id == product_id,
                    Product.status == ProductStatus.AVAILABLE,
                )
            )
            if product is None:
                raise ProductNotFoundError("商品不存在或尚未公开")
            return self._public_snapshot(product)

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
            return self._public_snapshot(product)

    def create_for_seller(
        self,
        *,
        seller_id: str,
        data: ProductCreateData,
    ) -> SellerProductInfo:
        with self._session_factory() as db, db.begin():
            product = Product(
                seller_id=seller_id,
                title=data.title,
                description=data.description,
                listed_price=data.listed_price,
                status=data.status,
            )
            product.policy = SellerPolicy(
                minimum_net_price=data.minimum_net_price,
                auto_accept_threshold=data.auto_accept_threshold,
                negotiation_style=data.negotiation_style,
                max_rounds=data.max_rounds,
                version=1,
            )
            db.add(product)
            db.flush()
            return self._seller_snapshot(product)

    def list_owned(self, *, seller_id: str) -> tuple[SellerProductInfo, ...]:
        with self._session_factory() as db:
            products = db.scalars(
                select(Product)
                .options(selectinload(Product.policy))
                .where(Product.seller_id == seller_id)
                .order_by(Product.id)
            )
            return tuple(self._seller_snapshot(product) for product in products)

    def get_owned_detail(
        self,
        *,
        product_id: int,
        seller_id: str,
    ) -> SellerProductInfo:
        with self._session_factory() as db:
            product = self._get_owned_product(
                db,
                product_id=product_id,
                seller_id=seller_id,
            )
            return self._seller_snapshot(product)

    def get_owned_product(self, *, product_id: int, seller_id: str) -> ProductInfo:
        """兼容只需要公开商品字段的所有权检查调用方。"""

        detail = self.get_owned_detail(product_id=product_id, seller_id=seller_id)
        return ProductInfo(
            id=detail.id,
            title=detail.title,
            description=detail.description,
            listed_price=detail.listed_price,
            status=detail.status,
        )

    def update_owned_product(
        self,
        *,
        product_id: int,
        seller_id: str,
        data: ProductUpdateData,
    ) -> SellerProductInfo:
        with self._session_factory() as db, db.begin():
            product = self._get_owned_product(
                db,
                product_id=product_id,
                seller_id=seller_id,
                for_update=True,
            )
            product.title = data.title
            product.description = data.description
            product.listed_price = data.listed_price
            product.status = data.status
            db.flush()
            return self._seller_snapshot(product)

    def update_owned_policy(
        self,
        *,
        product_id: int,
        seller_id: str,
        data: PolicyUpdateData,
    ) -> SellerProductInfo:
        with self._session_factory() as db, db.begin():
            product = self._get_owned_product(
                db,
                product_id=product_id,
                seller_id=seller_id,
                for_update=True,
            )
            policy = db.scalar(
                select(SellerPolicy)
                .where(SellerPolicy.product_id == product.id)
                .with_for_update()
            )
            if policy is None:
                raise PricingPolicyNotFoundError("商品尚未配置卖家规则")
            if policy.version != data.expected_version:
                raise PolicyVersionConflictError(
                    "卖家规则已经更新，请刷新后再提交"
                )
            policy.minimum_net_price = data.minimum_net_price
            policy.auto_accept_threshold = data.auto_accept_threshold
            policy.negotiation_style = data.negotiation_style
            policy.max_rounds = data.max_rounds
            policy.version += 1
            db.flush()
            return self._seller_snapshot(product)

    @staticmethod
    def _get_owned_product(
        db: Session,
        *,
        product_id: int,
        seller_id: str,
        for_update: bool = False,
    ) -> Product:
        statement = (
            select(Product)
            .options(selectinload(Product.policy))
            .where(
                Product.id == product_id,
                Product.seller_id == seller_id,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        product = db.scalar(statement)
        if product is None:
            raise ProductNotFoundError("商品不存在或当前卖家无权访问")
        return product

    @staticmethod
    def _public_snapshot(product: Product) -> ProductInfo:
        return ProductInfo(
            id=product.id,
            title=product.title,
            description=product.description,
            listed_price=product.listed_price,
            status=product.status,
        )

    @classmethod
    def _seller_snapshot(cls, product: Product) -> SellerProductInfo:
        policy = product.policy
        if policy is None:
            raise PricingPolicyNotFoundError("商品尚未配置卖家规则")
        public = cls._public_snapshot(product)
        return SellerProductInfo(
            id=public.id,
            title=public.title,
            description=public.description,
            listed_price=public.listed_price,
            status=public.status,
            policy=SellerPolicyInfo(
                minimum_net_price=policy.minimum_net_price,
                auto_accept_threshold=policy.auto_accept_threshold,
                negotiation_style=policy.negotiation_style,
                max_rounds=policy.max_rounds,
                version=policy.version,
            ),
        )
