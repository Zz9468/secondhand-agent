import argparse
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password, verify_password
from app.db.models import (
    Message,
    NegotiationSession,
    NegotiationStatus,
    NegotiationStyle,
    Offer,
    Product,
    ProductStatus,
    SellerAccount,
    SellerPolicy,
)
from app.db.session import get_engine

DEMO_PRODUCT_ID = 1001
DEMO_POLICY_ID = 1001
DEMO_SESSION_ID = 1001
DEMO_SELLER_ID = "demo-seller"
DEMO_SELLER_USERNAME = "demo-seller"
DEMO_BUYER_ID = "demo-buyer"


@dataclass(frozen=True, slots=True)
class SeedResult:
    product_id: int
    policy_id: int
    session_id: int


def seed_demo_data(
    db: Session,
    *,
    seller_password: str,
    reset_session: bool = False,
) -> SeedResult:
    """以固定主键补齐演示数据，重复执行不会创建重复记录。"""

    seller = db.get(SellerAccount, DEMO_SELLER_ID)
    if seller is None:
        seller = SellerAccount(
            id=DEMO_SELLER_ID,
            username=DEMO_SELLER_USERNAME,
            password_hash=hash_password(seller_password),
            is_active=True,
        )
        db.add(seller)
        db.flush()
    elif seller.username != DEMO_SELLER_USERNAME:
        raise RuntimeError("演示卖家 ID 已绑定其他用户名，拒绝覆盖现有账号")
    elif not seller.is_active or not verify_password(
        seller_password,
        seller.password_hash,
    ):
        # V1 升级产生的禁用占位账号，或显式修改的演示密码，在种子时启用。
        seller.password_hash = hash_password(seller_password)
        seller.is_active = True
        db.flush()

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

    if reset_session:
        # 先解除当前报价外键，再按固定演示会话精确清理，避免影响其他数据。
        negotiation.current_offer = None
        negotiation.confirmed_offer = None
        negotiation.confirmed_at = None
        negotiation.confirmation_request_id = None
        negotiation.confirmation_source = None
        negotiation.status = NegotiationStatus.ACTIVE
        negotiation.round_count = 0
        negotiation.version += 1
        db.flush()
        db.execute(delete(Message).where(Message.session_id == negotiation.id))
        db.execute(delete(Offer).where(Offer.session_id == negotiation.id))

    return SeedResult(
        product_id=product.id,
        policy_id=policy.id,
        session_id=negotiation.id,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 SecondHand Agent 演示数据")
    parser.add_argument(
        "--reset-session",
        action="store_true",
        help="清空演示会话的消息和报价，并恢复为可协商状态",
    )
    args = parser.parse_args()
    settings = get_settings()
    if settings.demo_seller_password is None:
        parser.error("请先在本地 .env 设置 DEMO_SELLER_PASSWORD")
    seller_password = settings.demo_seller_password.get_secret_value()
    if len(seller_password) < 12 or "CHANGE_ME" in seller_password.upper():
        parser.error("DEMO_SELLER_PASSWORD 必须替换为至少 12 个字符的真实密码")
    with Session(get_engine()) as db, db.begin():
        result = seed_demo_data(
            db,
            seller_password=seller_password,
            reset_session=args.reset_session,
        )

    print(
        "演示数据已就绪："
        f"seller_id={DEMO_SELLER_ID}, "
        f"product_id={result.product_id}, "
        f"policy_id={result.policy_id}, "
        f"session_id={result.session_id}, "
        f"session_reset={args.reset_session}"
    )


if __name__ == "__main__":
    main()
