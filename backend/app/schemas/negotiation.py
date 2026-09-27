from datetime import datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from app.agent.seller_agent import AgentTurnOutcome
from app.db.models import (
    ApprovalFollowupStatus,
    ApprovalStatus,
    ConfirmationSource,
    MessageRole,
    NegotiationStatus,
    ProductStatus,
)
from app.schemas.seller import PublicSellerSummaryResponse
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


class CreateNegotiationRequest(BaseModel):
    """阶段一用于替代固定买家会话的最小创建入口。"""

    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0)


class CreateNegotiationResponse(BaseModel):
    session_id: int
    created: bool


class ConfirmNegotiationRequest(BaseModel):
    """买家确认必须绑定当前报价和可重放的幂等键。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    offer_id: int = Field(gt=0)
    request_id: str = Field(
        min_length=8,
        max_length=56,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class CloseNegotiationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str = Field(
        min_length=8,
        max_length=56,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


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
    model_config = ConfigDict(from_attributes=True)

    id: int
    proposer: Literal["BUYER", "AGENT"]
    price: Decimal
    shipping_paid_by: ShippingPayer
    shipping_cost: Decimal | None
    seller_borne_discount: Decimal
    additional_terms: dict[str, JsonValue]
    status: Literal["PROPOSED", "ACCEPTED", "REJECTED", "WITHDRAWN"]
    expires_at: datetime | None
    created_at: datetime


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
    confirmed_offer_id: int | None
    confirmed_at: str | None
    confirmation_source: ConfirmationSource | None
    round_count: int
    version: int
    negotiation_style: str
    max_rounds: int
    recent_offers: list[OfferResponse]


class NegotiationDetailResponse(BaseModel):
    product: ProductResponse
    negotiation: NegotiationStateResponse


class BuyerNegotiationSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    product_title: str
    product_status: ProductStatus
    seller: PublicSellerSummaryResponse
    status: NegotiationStatus
    current_offer_id: int | None
    confirmed_offer_id: int | None
    round_count: int
    created_at: datetime
    updated_at: datetime


class BuyerNegotiationListResponse(BaseModel):
    negotiations: list[BuyerNegotiationSummaryResponse]


class SendMessageResponse(BaseModel):
    buyer_message: MessageResponse
    agent_message: MessageResponse
    outcome: AgentTurnOutcome | Literal["IDEMPOTENT_REPLAY"]
    formal_offer_id: int | None
    idempotent_replay: bool


class ConfirmNegotiationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: int
    status: Literal["AGREED"]
    confirmed_offer_id: int
    confirmed_at: datetime
    confirmation_source: ConfirmationSource
    system_message: MessageResponse
    idempotent_replay: bool


class CloseNegotiationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: int
    status: Literal["CLOSED"]
    cancelled_approval_id: int | None
    system_message: MessageResponse
    idempotent_replay: bool


class SellerNegotiationApprovalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    offer_id: int
    policy_version: int
    status: ApprovalStatus
    reason: str
    seller_comment: str | None
    expires_at: datetime
    reviewed_at: datetime | None
    followup_status: ApprovalFollowupStatus | None
    created_at: datetime
    updated_at: datetime


class SellerNegotiationMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: MessageRole
    content: str
    formal_offer_id: int | None
    agent_outcome: str | None
    created_at: datetime


class SellerNegotiationSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    product_title: str
    product_status: ProductStatus
    status: NegotiationStatus
    current_offer_id: int | None
    confirmed_offer_id: int | None
    confirmed_at: datetime | None
    confirmation_source: ConfirmationSource | None
    round_count: int
    version: int
    current_offer: OfferResponse | None
    confirmed_offer: OfferResponse | None
    latest_approval: SellerNegotiationApprovalResponse | None
    created_at: datetime
    updated_at: datetime


class SellerNegotiationListResponse(BaseModel):
    negotiations: list[SellerNegotiationSummaryResponse]


class SellerNegotiationDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    negotiation: SellerNegotiationSummaryResponse
    messages: list[SellerNegotiationMessageResponse]
    offers: list[OfferResponse]
    approvals: list[SellerNegotiationApprovalResponse]
