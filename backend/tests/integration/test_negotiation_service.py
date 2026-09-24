from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    NegotiationSession,
    Offer,
    OfferProposer,
    OfferStatus,
    SellerPolicy,
)
from app.services.errors import (
    NegotiationNotFoundError,
    OfferConflictError,
    OfferNotAuthorizedError,
)
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import OfferTerms, PriceZone, ShippingPayer
from app.services.product_service import ProductService
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration


def buyer_terms(price: str, *, shipping_cost: str | None = None) -> OfferTerms:
    return OfferTerms(
        buyer_payment=Decimal(price),
        shipping_paid_by=(
            ShippingPayer.SELLER if shipping_cost is not None else ShippingPayer.BUYER
        ),
        shipping_cost=Decimal(shipping_cost) if shipping_cost is not None else None,
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
        additional_terms={"delivery": "buyer_pickup"},
    )

    assert counter_offer.proposer is OfferProposer.AGENT
    assert counter_offer.status is OfferStatus.PROPOSED
    assert counter_offer.additional_terms == {"delivery": "buyer_pickup"}
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
