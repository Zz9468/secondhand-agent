from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.db.models import (
    NegotiationSession,
    NegotiationStatus,
    NegotiationStyle,
    Offer,
    OfferProposer,
    OfferStatus,
    Product,
    ProductStatus,
    SellerPolicy,
    UserAccount,
)
from app.services.auth_service import ensure_historical_buyer_account
from app.services.errors import (
    NegotiationLifecycleConflictError,
    NegotiationNotFoundError,
    OfferConflictError,
    OfferNotAuthorizedError,
)
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import OfferTerms, PriceZone, ShippingPayer
from app.services.product_service import ProductService
from tests.integration.factories import create_negotiation, create_user_account

pytestmark = pytest.mark.mysql_integration


def buyer_terms(price: str, *, shipping_cost: str | None = None) -> OfferTerms:
    return OfferTerms(
        buyer_payment=Decimal(price),
        shipping_paid_by=(
            ShippingPayer.SELLER if shipping_cost is not None else ShippingPayer.BUYER
        ),
        shipping_cost=Decimal(shipping_cost) if shipping_cost is not None else None,
    )


def test_create_session_reuses_active_unified_user_account(
    service_session_factory: sessionmaker[Session],
) -> None:
    existing_session_id, _ = create_negotiation(service_session_factory)
    with service_session_factory() as db:
        existing_session = db.get(NegotiationSession, existing_session_id)
        assert existing_session is not None
        product_id = existing_session.product_id

    buyer_id = create_user_account(service_session_factory)
    service = NegotiationService(service_session_factory)
    session_id, created = service.create_or_get_active_session(
        product_id=product_id,
        buyer_id=buyer_id,
    )
    repeated_id, repeated_created = service.create_or_get_active_session(
        product_id=product_id,
        buyer_id=buyer_id,
    )

    assert created is True
    assert repeated_created is False
    assert repeated_id == session_id
    with service_session_factory() as db:
        account = db.get(UserAccount, buyer_id)
        negotiation = db.get(NegotiationSession, session_id)
        assert account is not None
        assert account.is_active is True
        assert negotiation is not None
        assert negotiation.buyer_id == account.id


def test_disabled_historical_account_cannot_create_new_session(
    service_session_factory: sessionmaker[Session],
) -> None:
    existing_session_id, _ = create_negotiation(service_session_factory)
    historical_buyer_id = f"buyer-{uuid4().hex}"
    with service_session_factory() as db, db.begin():
        existing_session = db.get(NegotiationSession, existing_session_id)
        assert existing_session is not None
        product_id = existing_session.product_id
        ensure_historical_buyer_account(
            db,
            buyer_id=historical_buyer_id,
        )

    with pytest.raises(NegotiationLifecycleConflictError):
        NegotiationService(service_session_factory).create_or_get_active_session(
            product_id=product_id,
            buyer_id=historical_buyer_id,
        )


def test_user_cannot_create_negotiation_for_owned_product(
    service_session_factory: sessionmaker[Session],
) -> None:
    existing_session_id, _ = create_negotiation(service_session_factory)
    with service_session_factory() as db:
        existing_session = db.get(NegotiationSession, existing_session_id)
        assert existing_session is not None
        product = db.get(Product, existing_session.product_id)
        assert product is not None

    with pytest.raises(NegotiationLifecycleConflictError, match="自己发布的商品"):
        NegotiationService(service_session_factory).create_or_get_active_session(
            product_id=product.id,
            buyer_id=product.seller_id,
        )


@pytest.mark.parametrize(
    "terminal_status",
    [NegotiationStatus.AGREED, NegotiationStatus.CLOSED],
)
def test_terminal_session_is_not_restored_as_active(
    service_session_factory: sessionmaker[Session],
    terminal_status: NegotiationStatus,
) -> None:
    old_session_id, buyer_id = create_negotiation(service_session_factory)
    with service_session_factory() as db, db.begin():
        old_session = db.get(NegotiationSession, old_session_id)
        assert old_session is not None
        old_session.status = terminal_status
        product_id = old_session.product_id

    new_session_id, created = NegotiationService(
        service_session_factory
    ).create_or_get_active_session(
        product_id=product_id,
        buyer_id=buyer_id,
    )

    assert created is True
    assert new_session_id != old_session_id
    with service_session_factory() as db:
        new_session = db.get(NegotiationSession, new_session_id)
        assert new_session is not None
        assert new_session.status is NegotiationStatus.ACTIVE


def test_concurrent_session_creation_reuses_one_active_session(
    mysql_engine: Engine,
) -> None:
    committed_factory = sessionmaker(bind=mysql_engine, expire_on_commit=False)
    suffix = uuid4().hex
    seller_id = f"concurrent-seller-{suffix}"
    buyer_id = f"concurrent-buyer-{suffix}"
    with committed_factory() as db, db.begin():
        seller = UserAccount(
            id=seller_id,
            username=seller_id,
            display_name="并发测试卖家",
            password_hash=hash_password("concurrent-seller-password"),
            is_active=True,
        )
        buyer = UserAccount(
            id=buyer_id,
            username=buyer_id,
            display_name="并发测试买家",
            password_hash=hash_password("concurrent-buyer-password"),
            is_active=True,
        )
        product = Product(
            seller=seller,
            title="并发创建测试商品",
            description="验证重复点击只产生一个活动会话。",
            listed_price=Decimal("3000.00"),
            status=ProductStatus.AVAILABLE,
        )
        product.policy = SellerPolicy(
            minimum_net_price=Decimal("2700.00"),
            auto_accept_threshold=Decimal("2850.00"),
            negotiation_style=NegotiationStyle.BALANCED,
            max_rounds=6,
            version=1,
        )
        db.add_all([seller, buyer, product])
        db.flush()
        product_id = product.id

    barrier = Barrier(2)

    def create_session() -> tuple[int, bool]:
        barrier.wait()
        return NegotiationService(committed_factory).create_or_get_active_session(
            product_id=product_id,
            buyer_id=buyer_id,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _: create_session(), range(2)))

        assert len({session_id for session_id, _ in results}) == 1
        assert sorted(created for _, created in results) == [False, True]
        with committed_factory() as db:
            active_count = db.scalar(
                select(func.count())
                .select_from(NegotiationSession)
                .where(
                    NegotiationSession.product_id == product_id,
                    NegotiationSession.buyer_id == buyer_id,
                    NegotiationSession.status.in_(
                        (
                            NegotiationStatus.ACTIVE,
                            NegotiationStatus.WAITING_APPROVAL,
                        )
                    ),
                )
            )
            assert active_count == 1
    finally:
        with committed_factory() as db, db.begin():
            db.execute(
                delete(NegotiationSession).where(
                    NegotiationSession.product_id == product_id
                )
            )
            db.execute(delete(SellerPolicy).where(SellerPolicy.product_id == product_id))
            db.execute(delete(Product).where(Product.id == product_id))
            db.execute(
                delete(UserAccount).where(UserAccount.id.in_((seller_id, buyer_id)))
            )


def test_evaluate_offer_returns_three_authorization_zones(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    service = NegotiationService(service_session_factory)

    automatic = service.evaluate_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=buyer_terms("2850.00"),
    )
    approval = service.evaluate_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=buyer_terms("2800.00"),
    )
    prohibited = service.evaluate_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=buyer_terms("2699.99"),
    )

    assert automatic.zone is PriceZone.AUTO_ACCEPT
    assert automatic.can_accept_automatically is True
    assert approval.zone is PriceZone.APPROVAL_REQUIRED
    assert approval.can_request_approval is True
    assert prohibited.zone is PriceZone.PROHIBITED
    assert prohibited.is_acceptance_prohibited is True


def test_counter_offer_is_persisted_only_in_automatic_zone(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    service = NegotiationService(service_session_factory)
    buyer_offer = service.record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=buyer_terms("2800.00"),
    )

    with pytest.raises(OfferNotAuthorizedError):
        service.submit_counter_offer(
            session_id=session_id,
            buyer_id=buyer_id,
            terms=buyer_terms("2849.99"),
        )

    counter_offer = service.submit_counter_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=buyer_terms("2850.00"),
        additional_terms={"delivery_method": "pickup"},
    )

    assert counter_offer.proposer is OfferProposer.AGENT
    assert counter_offer.status is OfferStatus.PROPOSED
    assert counter_offer.additional_terms == {"delivery_method": "pickup"}
    with service_session_factory() as db:
        assert db.get(Offer, buyer_offer.id).status is OfferStatus.REJECTED
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        assert negotiation.current_offer_id == counter_offer.id
        count = db.scalar(
            select(func.count()).select_from(Offer).where(Offer.session_id == session_id)
        )
        assert count == 2


def test_accept_offer_reloads_latest_policy_and_only_accepts_current_buyer_offer(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    service = NegotiationService(service_session_factory)
    offer = service.record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=buyer_terms("2900.00"),
    )

    # 模拟评估后卖家提高阈值，接受操作必须重新读取而不能沿用旧结果。
    with service_session_factory() as db, db.begin():
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        policy = db.scalar(
            select(SellerPolicy).where(
                SellerPolicy.product_id == negotiation.product_id
            )
        )
        assert policy is not None
        policy.auto_accept_threshold = Decimal("2950.00")
        policy.version += 1

    with pytest.raises(OfferNotAuthorizedError):
        service.accept_offer(
            session_id=session_id,
            buyer_id=buyer_id,
            offer_id=offer.id,
        )

    with service_session_factory() as db, db.begin():
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        policy = db.scalar(
            select(SellerPolicy).where(
                SellerPolicy.product_id == negotiation.product_id
            )
        )
        assert policy is not None
        policy.auto_accept_threshold = Decimal("2850.00")
        policy.version += 1

    accepted = service.accept_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
    )
    assert accepted.status is OfferStatus.ACCEPTED

    with pytest.raises(OfferConflictError):
        service.accept_offer(
            session_id=session_id,
            buyer_id=buyer_id,
            offer_id=offer.id,
        )


def test_seller_paid_shipping_is_included_in_authorization(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    service = NegotiationService(service_session_factory)

    result = service.evaluate_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=buyer_terms("2900.00", shipping_cost="100.00"),
    )

    assert result.zone is PriceZone.APPROVAL_REQUIRED
    assert result.can_accept_automatically is False


def test_services_hide_private_policy_and_reject_cross_buyer_access(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    product_service = ProductService(service_session_factory)
    negotiation_service = NegotiationService(service_session_factory)

    product = product_service.get_for_negotiation(
        session_id=session_id,
        buyer_id=buyer_id,
    )
    assert product.title == "阶段四测试商品"
    assert not hasattr(product, "minimum_net_price")

    with pytest.raises(NegotiationNotFoundError):
        negotiation_service.get_state(
            session_id=session_id,
            buyer_id="another-buyer",
        )


def test_unknown_additional_terms_can_be_recorded_but_never_auto_authorized(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    service = NegotiationService(service_session_factory)
    additional_terms = {"ship_by": "today"}

    authorization = service.evaluate_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=buyer_terms("3000.00"),
        additional_terms=additional_terms,
    )
    buyer_offer = service.record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=buyer_terms("3000.00"),
        additional_terms=additional_terms,
    )

    assert authorization.conditions_valid is False
    assert authorization.can_accept_automatically is False
    assert buyer_offer.additional_terms == additional_terms
    with pytest.raises(OfferNotAuthorizedError):
        service.accept_offer(
            session_id=session_id,
            buyer_id=buyer_id,
            offer_id=buyer_offer.id,
        )
