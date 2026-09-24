from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.services.pricing_service import ShippingPayer


class NegotiationAction(StrEnum):
    INQUIRY = "INQUIRY"
    ACCEPT = "ACCEPT"
    COUNTER = "COUNTER"
    REJECT = "REJECT"
    REQUEST_APPROVAL = "REQUEST_APPROVAL"
    CLARIFY = "CLARIFY"


class NegotiationDecision(BaseModel):
    """模型内部决策契约，不能直接作为买家响应返回。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: NegotiationAction
    offer_id: int | None = Field(default=None, gt=0)
    proposed_price: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=12,
        decimal_places=2,
    )
    shipping_paid_by: ShippingPayer | None = None
    shipping_cost: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=12,
        decimal_places=2,
    )
    seller_borne_discount: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=12,
        decimal_places=2,
    )
    additional_terms: dict[str, JsonValue] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=500)
    reply: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def validate_action_fields(self) -> Self:
        if self.action is NegotiationAction.COUNTER:
            if self.proposed_price is None or self.shipping_paid_by is None:
                raise ValueError("COUNTER requires proposed_price and shipping_paid_by")
            if self.offer_id is not None:
                raise ValueError("COUNTER cannot include offer_id")
            return self

        if self.action in {
            NegotiationAction.ACCEPT,
            NegotiationAction.REQUEST_APPROVAL,
        }:
            if self.offer_id is None:
                raise ValueError(f"{self.action.value} requires offer_id")
        elif self.offer_id is not None:
            raise ValueError(f"{self.action.value} cannot include offer_id")

        if any(
            value is not None
            for value in (
                self.proposed_price,
                self.shipping_paid_by,
                self.shipping_cost,
                self.seller_borne_discount,
            )
        ) or self.additional_terms:
            raise ValueError(f"{self.action.value} cannot include counter-offer terms")
        return self
