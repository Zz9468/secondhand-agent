from decimal import Decimal

import pytest

from app.services.pricing_service import (
    InvalidMoneyError,
    InvalidPricingPolicyError,
    OfferTerms,
    PriceZone,
    PricingError,
    PricingPolicy,
    ShippingPayer,
    UnknownCostError,
    calculate_net_income,
    evaluate_offer,
)


@pytest.fixture
def policy() -> PricingPolicy:
    return PricingPolicy(
        minimum_net_price=Decimal("2700.00"),
        auto_accept_threshold=Decimal("2850.00"),
    )


@pytest.mark.parametrize(
    ("buyer_payment", "expected_zone"),
    [
        ("3000.00", PriceZone.AUTO_ACCEPT),
        ("2850.00", PriceZone.AUTO_ACCEPT),
        ("2849.99", PriceZone.APPROVAL_REQUIRED),
        ("2700.00", PriceZone.APPROVAL_REQUIRED),
        ("2699.99", PriceZone.PROHIBITED),
    ],
)
def test_evaluate_offer_covers_price_zones_and_boundaries(
    policy: PricingPolicy,
    buyer_payment: str,
    expected_zone: PriceZone,
) -> None:
    terms = OfferTerms(
        buyer_payment=Decimal(buyer_payment),
        shipping_paid_by=ShippingPayer.BUYER,
    )

    evaluation = evaluate_offer(terms=terms, policy=policy)

    assert evaluation.net_income == Decimal(buyer_payment)
    assert evaluation.zone is expected_zone


def test_seller_paid_shipping_and_discount_reduce_net_income(
    policy: PricingPolicy,
) -> None:
    terms = OfferTerms(
        buyer_payment=Decimal("2900.00"),
        shipping_paid_by=ShippingPayer.SELLER,
        shipping_cost=Decimal("40.00"),
        seller_borne_discount=Decimal("10.00"),
    )

    evaluation = evaluate_offer(terms=terms, policy=policy)

    assert evaluation.net_income == Decimal("2850.00")
    assert evaluation.zone is PriceZone.AUTO_ACCEPT


def test_buyer_paid_shipping_is_not_deducted_from_seller_income(
    policy: PricingPolicy,
) -> None:
    terms = OfferTerms(
        buyer_payment=Decimal("2850.00"),
        shipping_paid_by=ShippingPayer.BUYER,
        shipping_cost=Decimal("50.00"),
    )

    evaluation = evaluate_offer(terms=terms, policy=policy)

    assert evaluation.net_income == Decimal("2850.00")
    assert evaluation.can_accept_automatically is True
    assert evaluation.can_request_approval is False
    assert evaluation.is_acceptance_prohibited is False


def test_approval_zone_exposes_only_approval_permission(policy: PricingPolicy) -> None:
    terms = OfferTerms(
        buyer_payment=Decimal("2750.00"),
        shipping_paid_by=ShippingPayer.BUYER,
    )

    evaluation = evaluate_offer(terms=terms, policy=policy)

    assert evaluation.can_accept_automatically is False
    assert evaluation.can_request_approval is True
    assert evaluation.is_acceptance_prohibited is False


def test_prohibited_zone_cannot_be_approved(policy: PricingPolicy) -> None:
    terms = OfferTerms(
        buyer_payment=Decimal("2600.00"),
        shipping_paid_by=ShippingPayer.BUYER,
    )

    evaluation = evaluate_offer(terms=terms, policy=policy)

    assert evaluation.can_accept_automatically is False
    assert evaluation.can_request_approval is False
    assert evaluation.is_acceptance_prohibited is True


@pytest.mark.parametrize(
    ("terms", "unknown_field"),
    [
        (
            OfferTerms(
                buyer_payment=Decimal("2900.00"),
                shipping_paid_by=ShippingPayer.SELLER,
                shipping_cost=None,
            ),
            "shipping_cost",
        ),
        (
            OfferTerms(
                buyer_payment=Decimal("2900.00"),
                shipping_paid_by=ShippingPayer.BUYER,
                seller_borne_discount=None,
            ),
            "seller_borne_discount",
        ),
    ],
)
def test_unknown_seller_cost_prevents_evaluation(
    policy: PricingPolicy,
    terms: OfferTerms,
    unknown_field: str,
) -> None:
    with pytest.raises(UnknownCostError, match=unknown_field):
        evaluate_offer(terms=terms, policy=policy)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (Decimal("-0.01"), "cannot be negative"),
        (Decimal("100.001"), "more than two decimal places"),
        (Decimal("0.0000000000000000000000000001"), "more than two decimal places"),
        (Decimal("NaN"), "must be finite"),
        (Decimal("Infinity"), "must be finite"),
        (Decimal("10000000000.00"), "exceeds DECIMAL"),
    ],
)
def test_invalid_money_is_rejected(value: Decimal, message: str) -> None:
    with pytest.raises(InvalidMoneyError, match=message):
        OfferTerms(buyer_payment=value, shipping_paid_by=ShippingPayer.BUYER)


def test_non_decimal_money_is_rejected() -> None:
    with pytest.raises(InvalidMoneyError, match="must be a Decimal"):
        OfferTerms(  # type: ignore[arg-type]
            buyer_payment=2850.0,
            shipping_paid_by=ShippingPayer.BUYER,
        )


def test_trailing_zeroes_are_normalized_to_two_decimal_places() -> None:
    terms = OfferTerms(
        buyer_payment=Decimal("2850.0000"),
        shipping_paid_by=ShippingPayer.BUYER,
    )

    assert terms.buyer_payment == Decimal("2850.00")
    assert terms.buyer_payment.as_tuple().exponent == -2


def test_invalid_shipping_payer_is_rejected() -> None:
    with pytest.raises(PricingError, match="must be a ShippingPayer"):
        OfferTerms(  # type: ignore[arg-type]
            buyer_payment=Decimal("2850.00"),
            shipping_paid_by="buyer",
        )


def test_threshold_cannot_be_lower_than_minimum_price() -> None:
    with pytest.raises(InvalidPricingPolicyError, match="cannot be lower"):
        PricingPolicy(
            minimum_net_price=Decimal("2850.00"),
            auto_accept_threshold=Decimal("2700.00"),
        )


def test_calculate_net_income_is_independent_from_offer_evaluation() -> None:
    terms = OfferTerms(
        buyer_payment=Decimal("2888.88"),
        shipping_paid_by=ShippingPayer.SELLER,
        shipping_cost=Decimal("18.88"),
    )

    assert calculate_net_income(terms) == Decimal("2870.00")
