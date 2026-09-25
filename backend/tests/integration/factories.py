from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.db.models import (
    NegotiationSession,
    NegotiationStatus,
    NegotiationStyle,
    Product,
    ProductStatus,
    SellerAccount,
    SellerPolicy,
)


def create_negotiation(
    session_factory: sessionmaker[Session],
    *,
    minimum_net_price: Decimal = Decimal("2700.00"),
    auto_accept_threshold: Decimal = Decimal("2850.00"),
    max_rounds: int = 6,
) -> tuple[int, str]:
    """为集成测试创建相互隔离的商品、规则和会话。"""

    suffix = uuid4().hex
    with session_factory() as db, db.begin():
        seller = SellerAccount(
            id=f"seller-{suffix}",
            username=f"seller-{suffix}",
            password_hash=hash_password("integration-test-password"),
            is_active=True,
        )
        product = Product(
            seller=seller,
            title="阶段四测试商品",
            description="验证业务服务与 Agent 工具的数据库约束。",
            listed_price=Decimal("3000.00"),
            status=ProductStatus.AVAILABLE,
        )
        product.policy = SellerPolicy(
            minimum_net_price=minimum_net_price,
            auto_accept_threshold=auto_accept_threshold,
            negotiation_style=NegotiationStyle.BALANCED,
            max_rounds=max_rounds,
            version=1,
        )
        negotiation = NegotiationSession(
            product=product,
            buyer_id=f"buyer-{suffix}",
            status=NegotiationStatus.ACTIVE,
            round_count=0,
            version=1,
        )
        db.add_all([seller, negotiation])
        db.flush()
        return negotiation.id, negotiation.buyer_id
