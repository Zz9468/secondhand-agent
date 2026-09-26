from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models import (
    ConfirmationSource,
    Message,
    MessageRole,
    NegotiationSession,
    NegotiationStatus,
    NegotiationStyle,
    Offer,
    OfferProposer,
    OfferStatus,
    Product,
    ProductStatus,
    SellerAccount,
    SellerPolicy,
    ShippingPayer,
)
from scripts.seed_data import (
    DEMO_PRODUCT_ID,
    DEMO_SESSION_ID,
    seed_demo_data,
)

pytestmark = pytest.mark.mysql_integration


def test_business_models_support_crud_and_exact_money(db_session: Session) -> None:
    unique_suffix = uuid4().hex
    seller = SellerAccount(
        id=f"seller-{unique_suffix}",
        username=f"seller-{unique_suffix}",
        password_hash=hash_password("integration-test-password"),
        is_active=True,
    )
    product = Product(
        seller=seller,
        title="集成测试商品",
        description="用于验证 ORM 的临时数据。",
        listed_price=Decimal("3000.10"),
        status=ProductStatus.AVAILABLE,
    )
    product.policy = SellerPolicy(
        minimum_net_price=Decimal("2700.20"),
        auto_accept_threshold=Decimal("2850.30"),
        negotiation_style=NegotiationStyle.BALANCED,
        max_rounds=6,
        version=1,
    )
    negotiation = NegotiationSession(
        product=product,
        buyer_id=f"buyer-{unique_suffix}",
        status=NegotiationStatus.ACTIVE,
        round_count=1,
        version=1,
    )
    message = Message(
        session=negotiation,
        role=MessageRole.BUYER,
        content="2800 元包邮可以吗？",
        request_id=f"request-{unique_suffix}",
    )
    offer = Offer(
        session=negotiation,
        proposer=OfferProposer.BUYER,
        price=Decimal("2800.10"),
        shipping_paid_by=ShippingPayer.SELLER,
        shipping_cost=Decimal("20.05"),
        seller_borne_discount=Decimal("0.15"),
        terms={"delivery": "快递"},
        status=OfferStatus.PROPOSED,
    )
    db_session.add_all([seller, product, negotiation, message, offer])
    db_session.flush()

    negotiation.current_offer = offer
    product.title = "已更新的集成测试商品"
    db_session.flush()
    db_session.expire_all()

    stored_product = db_session.get(Product, product.id)
    stored_offer = db_session.get(Offer, offer.id)
    assert stored_product is not None
    assert stored_product.title == "已更新的集成测试商品"
    assert stored_product.listed_price == Decimal("3000.10")
    assert stored_product.policy is not None
    assert stored_product.policy.minimum_net_price == Decimal("2700.20")
    assert stored_offer is not None
    assert stored_offer.price == Decimal("2800.10")
    assert stored_offer.shipping_cost == Decimal("20.05")
    assert stored_offer.seller_borne_discount == Decimal("0.15")
    assert stored_offer.terms == {"delivery": "快递"}

    stored_message = db_session.scalar(
        select(Message).where(Message.request_id == f"request-{unique_suffix}")
    )
    assert stored_message is not None
    message_id = stored_message.id
    db_session.delete(stored_message)
    db_session.flush()
    assert db_session.get(Message, message_id) is None


def test_seed_data_is_idempotent(db_session: Session) -> None:
    first = seed_demo_data(db_session, seller_password="demo-test-password")
    second = seed_demo_data(db_session, seller_password="demo-test-password")
    db_session.flush()

    assert first == second
    assert first.product_id == DEMO_PRODUCT_ID
    assert first.session_id == DEMO_SESSION_ID
    assert db_session.get(Product, DEMO_PRODUCT_ID) is not None
    assert db_session.get(NegotiationSession, DEMO_SESSION_ID) is not None


def test_seed_data_can_explicitly_reset_only_the_demo_session(
    db_session: Session,
) -> None:
    seed_demo_data(
        db_session,
        seller_password="demo-test-password",
        reset_session=True,
    )
    negotiation = db_session.get(NegotiationSession, DEMO_SESSION_ID)
    assert negotiation is not None
    offer = Offer(
        session_id=negotiation.id,
        proposer=OfferProposer.BUYER,
        price=Decimal("2800.00"),
        shipping_paid_by=ShippingPayer.BUYER,
        seller_borne_discount=Decimal("0.00"),
        terms={},
        status=OfferStatus.PROPOSED,
    )
    message = Message(
        session_id=negotiation.id,
        role=MessageRole.BUYER,
        content="用于验证显式重置。",
        request_id=f"reset-{uuid4().hex}",
    )
    db_session.add_all([offer, message])
    db_session.flush()
    negotiation.current_offer = offer
    negotiation.confirmed_offer = offer
    negotiation.confirmed_at = datetime.now()
    negotiation.confirmation_request_id = f"confirm-reset-{uuid4().hex}"
    negotiation.confirmation_source = ConfirmationSource.AUTO_ACCEPTED_BUYER_OFFER
    negotiation.round_count = 1
    negotiation.status = NegotiationStatus.AGREED
    previous_version = negotiation.version
    db_session.flush()

    seed_demo_data(
        db_session,
        seller_password="demo-test-password",
        reset_session=True,
    )
    db_session.flush()

    assert negotiation.current_offer_id is None
    assert negotiation.confirmed_offer_id is None
    assert negotiation.confirmed_at is None
    assert negotiation.confirmation_request_id is None
    assert negotiation.confirmation_source is None
    assert negotiation.round_count == 0
    assert negotiation.status is NegotiationStatus.ACTIVE
    assert negotiation.version == previous_version + 1
    assert list(
        db_session.scalars(select(Message).where(Message.session_id == negotiation.id))
    ) == []
    assert list(
        db_session.scalars(select(Offer).where(Offer.session_id == negotiation.id))
    ) == []
