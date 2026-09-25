from datetime import datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from app.agent.seller_agent import AgentTurnOutcome
from app.db.models import MessageRole
from app.services.pricing_service import ShippingPayer


class BuyerOfferRequest(BaseModel):
    """买家随聊天消息明确提交的结构化报价。"""

    model_config = ConfigDict(extra="forbid")

    price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    shipping_paid_by: ShippingPayer
    shipping_cost: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=12,
        decimal_places=2,
    )
    delivery_method: Literal["shipping", "pickup"] | None = None

    @model_validator(mode="after")
    def validate_shipping_terms(self) -> Self:
        if self.shipping_paid_by is ShippingPayer.SELLER and self.shipping_cost is None:
            raise ValueError("卖家承担运费时必须填写运费金额")
        if self.delivery_method == "pickup":
            if self.shipping_paid_by is ShippingPayer.SELLER:
                raise ValueError("面交不能要求卖家承担运费")
            if self.shipping_cost not in {None, Decimal("0")}:
                raise ValueError("面交不能包含非零运费")
        return self


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str = Field(
        min_length=8,
        max_length=56,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    content: str = Field(min_length=1, max_length=4000)
    offer: BuyerOfferRequest | None = None

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("消息内容不能为空")
        return value.strip()


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: MessageRole
    content: str
    request_id: str
    created_at: datetime


class MessageListResponse(BaseModel):
    messages: list[MessageResponse]


class OfferResponse(BaseModel):
    id: int
    proposer: Literal["BUYER", "AGENT"]
    price: str
    shipping_paid_by: ShippingPayer
    shipping_cost: str | None
    seller_borne_discount: str
    additional_terms: dict[str, JsonValue]
    status: Literal["PROPOSED", "ACCEPTED", "REJECTED", "WITHDRAWN"]
    expires_at: str | None
    created_at: str


class ProductResponse(BaseModel):
    id: int
    title: str
    description: str
    listed_price: str
    status: str


class NegotiationStateResponse(BaseModel):
    id: int
    product_id: int
    status: str
    current_offer_id: int | None
    round_count: int
    version: int
    negotiation_style: str
    max_rounds: int
    recent_offers: list[OfferResponse]


class NegotiationDetailResponse(BaseModel):
    product: ProductResponse
    negotiation: NegotiationStateResponse


class SendMessageResponse(BaseModel):
    buyer_message: MessageResponse
    agent_message: MessageResponse
    outcome: AgentTurnOutcome | Literal["IDEMPOTENT_REPLAY"]
    formal_offer_id: int | None
    idempotent_replay: bool
