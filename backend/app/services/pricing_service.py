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


class PricingService:
    """向业务编排层提供无数据库依赖的价格授权判断。"""

    def evaluate(self, *, terms: OfferTerms, policy: PricingPolicy) -> OfferEvaluation:
        return evaluate_offer(terms=terms, policy=policy)


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
