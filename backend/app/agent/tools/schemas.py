from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.services.pricing_service import OfferTerms, ShippingPayer


class OfferTermsInput(BaseModel):
    """模型可提交的报价字段；会话和身份由服务端上下文绑定。"""

    model_config = ConfigDict(extra="forbid")

    price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    shipping_paid_by: ShippingPayer
    shipping_cost: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=12,
        decimal_places=2,
    )
    seller_borne_discount: Decimal = Field(
        default=Decimal("0.00"),
        ge=0,
        max_digits=12,
        decimal_places=2,
    )
    additional_terms: dict[str, JsonValue] = Field(default_factory=dict)

    def to_domain(self) -> OfferTerms:
        return OfferTerms(
            buyer_payment=self.price,
            shipping_paid_by=self.shipping_paid_by,
            shipping_cost=self.shipping_cost,
            seller_borne_discount=self.seller_borne_discount,
        )


class AcceptOfferInput(BaseModel):
    """接受报价时只允许模型选择数据库中已有的报价编号。"""

    model_config = ConfigDict(extra="forbid")

    offer_id: int = Field(gt=0)
