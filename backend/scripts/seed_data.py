from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models import (
    NegotiationSession,
    NegotiationStatus,
    NegotiationStyle,
    Product,
    ProductStatus,
    SellerPolicy,
)
from app.db.session import get_engine

DEMO_PRODUCT_ID = 1001
DEMO_POLICY_ID = 1001
DEMO_SESSION_ID = 1001
DEMO_SELLER_ID = "demo-seller"
DEMO_BUYER_ID = "demo-buyer"


@dataclass(frozen=True, slots=True)
class SeedResult:
    product_id: int
    policy_id: int
    session_id: int


def seed_demo_data(db: Session) -> SeedResult:
    """以固定主键补齐演示数据，重复执行不会创建重复记录。"""

    product = db.get(Product, DEMO_PRODUCT_ID)
    if product is None:
        product = Product(
            id=DEMO_PRODUCT_ID,
            seller_id=DEMO_SELLER_ID,
            title="iPhone 14 Pro 256GB",
            description="95 新，电池健康度 89%，无拆修，配件齐全。",
            listed_price=Decimal("3000.00"),
            status=ProductStatus.AVAILABLE,
        )
        db.add(product)
        db.flush()
    elif product.seller_id != DEMO_SELLER_ID:
        raise RuntimeError("演示商品 ID 已被其他卖家占用，拒绝覆盖现有数据")

    policy = db.get(SellerPolicy, DEMO_POLICY_ID)
    if policy is None:
        policy = SellerPolicy(
            id=DEMO_POLICY_ID,
            product_id=product.id,
            minimum_net_price=Decimal("2700.00"),
            auto_accept_threshold=Decimal("2850.00"),
            negotiation_style=NegotiationStyle.BALANCED,
            max_rounds=6,
            version=1,
        )
        db.add(policy)
        db.flush()
    elif policy.product_id != product.id:
        raise RuntimeError("演示规则 ID 已绑定其他商品，拒绝覆盖现有数据")

    negotiation = db.get(NegotiationSession, DEMO_SESSION_ID)
    if negotiation is None:
        negotiation = NegotiationSession(
            id=DEMO_SESSION_ID,
            product_id=product.id,
            buyer_id=DEMO_BUYER_ID,
            status=NegotiationStatus.ACTIVE,
            round_count=0,
            version=1,
        )
        db.add(negotiation)
        db.flush()
    elif (
        negotiation.product_id != product.id
        or negotiation.buyer_id != DEMO_BUYER_ID
    ):
        raise RuntimeError("演示会话 ID 已用于其他买家或商品，拒绝覆盖现有数据")

    return SeedResult(
        product_id=product.id,
        policy_id=policy.id,
        session_id=negotiation.id,
    )


def main() -> None:
    with Session(get_engine()) as db, db.begin():
        result = seed_demo_data(db)

    print(
        "演示数据已就绪："
        f"seller_id={DEMO_SELLER_ID}, "
        f"product_id={result.product_id}, "
        f"policy_id={result.policy_id}, "
        f"session_id={result.session_id}"
    )


if __name__ == "__main__":
    main()
