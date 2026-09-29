from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

MONEY_QUANTUM = Decimal("0.01")
ZERO_MONEY = Decimal("0.00")
MAX_MONEY = Decimal("9999999999.99")


class PricingError(ValueError):
    """价格输入无效或无法计算时抛出的基础异常。"""


class InvalidMoneyError(PricingError):
    """金额无法被安全表示时抛出的异常。"""


class InvalidPricingPolicyError(PricingError):
    """价格阈值相互矛盾时抛出的异常。"""


class UnknownCostError(PricingError):
    """无法准确计算卖家净收入时抛出的异常。"""


class ShippingPayer(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"


class PriceZone(StrEnum):
    AUTO_ACCEPT = "AUTO_ACCEPT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    PROHIBITED = "PROHIBITED"


def _validate_money(value: Decimal, *, field_name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise InvalidMoneyError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise InvalidMoneyError(f"{field_name} must be finite")
    if value < ZERO_MONEY:
        raise InvalidMoneyError(f"{field_name} cannot be negative")
    if value > MAX_MONEY:
        raise InvalidMoneyError(f"{field_name} exceeds DECIMAL(12,2)")
    if value.normalize().as_tuple().exponent < -2:
        raise InvalidMoneyError(f"{field_name} cannot have more than two decimal places")
    return value.quantize(MONEY_QUANTUM)


@dataclass(frozen=True, slots=True)
class PricingPolicy:
    minimum_net_price: Decimal
    auto_accept_threshold: Decimal

    def __post_init__(self) -> None:
        minimum_net_price = _validate_money(
            self.minimum_net_price,
            field_name="minimum_net_price",
        )
        auto_accept_threshold = _validate_money(
            self.auto_accept_threshold,
            field_name="auto_accept_threshold",
        )
        if auto_accept_threshold < minimum_net_price:
            raise InvalidPricingPolicyError(
                "auto_accept_threshold cannot be lower than minimum_net_price"
            )

        object.__setattr__(self, "minimum_net_price", minimum_net_price)
        object.__setattr__(self, "auto_accept_threshold", auto_accept_threshold)


@dataclass(frozen=True, slots=True)
class OfferTerms:
    buyer_payment: Decimal
    shipping_paid_by: ShippingPayer
    shipping_cost: Decimal | None = None
    seller_borne_discount: Decimal | None = ZERO_MONEY

    def __post_init__(self) -> None:
        if not isinstance(self.shipping_paid_by, ShippingPayer):
            raise PricingError("shipping_paid_by must be a ShippingPayer")

        object.__setattr__(
            self,
            "buyer_payment",
            _validate_money(self.buyer_payment, field_name="buyer_payment"),
        )
        if self.shipping_cost is not None:
            object.__setattr__(
                self,
                "shipping_cost",
                _validate_money(self.shipping_cost, field_name="shipping_cost"),
            )
        if self.seller_borne_discount is not None:
            object.__setattr__(
                self,
                "seller_borne_discount",
                _validate_money(
                    self.seller_borne_discount,
                    field_name="seller_borne_discount",
                ),
            )


@dataclass(frozen=True, slots=True)
class OfferEvaluation:
    net_income: Decimal
    zone: PriceZone

    @property
    def can_accept_automatically(self) -> bool:
        return self.zone is PriceZone.AUTO_ACCEPT

    @property
    def can_request_approval(self) -> bool:
        return self.zone is PriceZone.APPROVAL_REQUIRED

    @property
    def is_acceptance_prohibited(self) -> bool:
        return self.zone is PriceZone.PROHIBITED


@dataclass(frozen=True, slots=True)
class OfferAuthorization:
    """生产编排与离线评测共用的正式报价授权事实。"""

    zone: PriceZone
    conditions_valid: bool
    can_accept_automatically: bool
    can_submit_counter_offer: bool
    can_request_approval: bool
    is_acceptance_prohibited: bool
    reason_code: str


class PricingService:
    """向业务编排层提供无数据库依赖的价格授权判断。"""

    def evaluate(self, *, terms: OfferTerms, policy: PricingPolicy) -> OfferEvaluation:
        return evaluate_offer(terms=terms, policy=policy)

    def authorize(
        self,
        *,
        terms: OfferTerms,
        policy: PricingPolicy,
        additional_terms: Mapping[str, object] | None = None,
    ) -> OfferAuthorization:
        return authorize_offer(
            terms=terms,
            policy=policy,
            additional_terms=additional_terms,
        )


def calculate_net_income(terms: OfferTerms) -> Decimal:
    """准确计算卖家净收入；卖家承担的成本未知时拒绝计算。"""

    if terms.shipping_paid_by is ShippingPayer.SELLER:
        if terms.shipping_cost is None:
            raise UnknownCostError("shipping_cost is required when the seller pays shipping")
        seller_shipping_cost = terms.shipping_cost
    else:
        seller_shipping_cost = ZERO_MONEY

    if terms.seller_borne_discount is None:
        raise UnknownCostError("seller_borne_discount must be known")

    return (
        terms.buyer_payment - seller_shipping_cost - terms.seller_borne_discount
    ).quantize(MONEY_QUANTUM)


def evaluate_offer(*, terms: OfferTerms, policy: PricingPolicy) -> OfferEvaluation:
    """按卖家净收入划分报价区间，不在此处作出协商策略决策。"""

    net_income = calculate_net_income(terms)
    if net_income >= policy.auto_accept_threshold:
        zone = PriceZone.AUTO_ACCEPT
    elif net_income >= policy.minimum_net_price:
        zone = PriceZone.APPROVAL_REQUIRED
    else:
        zone = PriceZone.PROHIBITED

    return OfferEvaluation(net_income=net_income, zone=zone)


def additional_terms_are_authorized(
    additional_terms: Mapping[str, object] | None,
) -> bool:
    """只自动承诺系统能够验证的配送方式。"""

    if not additional_terms:
        return True
    if set(additional_terms) != {"delivery_method"}:
        return False
    return additional_terms["delivery_method"] in {"shipping", "pickup"}


def authorize_offer(
    *,
    terms: OfferTerms,
    policy: PricingPolicy,
    additional_terms: Mapping[str, object] | None = None,
) -> OfferAuthorization:
    """把价格区间与可验证交易条件组合为唯一授权契约。"""

    evaluation = evaluate_offer(terms=terms, policy=policy)
    conditions_valid = additional_terms_are_authorized(additional_terms)
    if not conditions_valid:
        return OfferAuthorization(
            zone=evaluation.zone,
            conditions_valid=False,
            can_accept_automatically=False,
            can_submit_counter_offer=False,
            can_request_approval=False,
            is_acceptance_prohibited=True,
            reason_code="UNSUPPORTED_ADDITIONAL_TERMS",
        )

    reason_codes = {
        PriceZone.AUTO_ACCEPT: "AUTO_AUTHORIZED",
        PriceZone.APPROVAL_REQUIRED: "SELLER_APPROVAL_REQUIRED",
        PriceZone.PROHIBITED: "BELOW_MINIMUM_NET_INCOME",
    }
    return OfferAuthorization(
        zone=evaluation.zone,
        conditions_valid=True,
        can_accept_automatically=evaluation.can_accept_automatically,
        can_submit_counter_offer=evaluation.can_accept_automatically,
        can_request_approval=evaluation.can_request_approval,
        is_acceptance_prohibited=evaluation.is_acceptance_prohibited,
        reason_code=reason_codes[evaluation.zone],
    )
