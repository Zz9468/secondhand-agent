from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password, verify_password
from app.db.models import (
    NegotiationStyle,
    Product,
    ProductStatus,
    SellerPolicy,
    UserAccount,
)
from app.db.session import get_engine

DEMO_PRODUCT_ID = 1001
DEMO_POLICY_ID = 1001
DEMO_SELLER_ID = "demo-seller"
DEMO_SELLER_USERNAME = "demo-seller"
DEMO_SELLER_DISPLAY_NAME = "演示卖家"


@dataclass(frozen=True, slots=True)
class SeedResult:
    product_id: int
    policy_id: int


def seed_demo_data(
    db: Session,
    *,
    seller_password: str,
) -> SeedResult:
    """幂等补齐演示账号、商品和策略，不代替买家创建协商会话。"""

    seller = db.get(UserAccount, DEMO_SELLER_ID)
    if seller is None:
        seller = UserAccount(
            id=DEMO_SELLER_ID,
            username=DEMO_SELLER_USERNAME,
            display_name=DEMO_SELLER_DISPLAY_NAME,
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
    if seller.display_name != DEMO_SELLER_DISPLAY_NAME:
        seller.display_name = DEMO_SELLER_DISPLAY_NAME
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

    return SeedResult(
        product_id=product.id,
        policy_id=policy.id,
    )


def main() -> None:
    settings = get_settings()
    if settings.demo_seller_password is None:
        raise SystemExit("请先在本地 .env 设置 DEMO_SELLER_PASSWORD")
    seller_password = settings.demo_seller_password.get_secret_value()
    if len(seller_password) < 12 or "CHANGE_ME" in seller_password.upper():
        raise SystemExit("DEMO_SELLER_PASSWORD 必须替换为至少 12 个字符的真实密码")
    with Session(get_engine()) as db, db.begin():
        result = seed_demo_data(
            db,
            seller_password=seller_password,
        )

    print(
        "演示数据已就绪："
        f"seller_id={DEMO_SELLER_ID}, "
        f"seller_username={DEMO_SELLER_USERNAME}, "
        f"product_id={result.product_id}, "
        f"policy_id={result.policy_id}。"
        "协商会话只会在买家明确点击“与卖家协商”后创建。"
    )


if __name__ == "__main__":
    main()
