from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from app.db.models import (
    ApprovalFollowupStatus,
    ApprovalStatus,
    NegotiationStatus,
    OfferProposer,
    OfferStatus,
    ShippingPayer,
)


class CreateApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    offer_id: int = Field(gt=0)
    expected_policy_version: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=2000)
    expires_at: datetime

    @field_validator("reason")
    @classmethod
    def reject_blank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("审批原因不能为空")
        return value.strip()


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: int
    offer_id: int
    policy_version: int
    status: ApprovalStatus
    reason: str
    seller_comment: str | None
    expires_at: datetime
    reviewed_at: datetime | None
    followup_status: ApprovalFollowupStatus | None
    followup_request_id: str | None
    created_at: datetime
    updated_at: datetime


class ApprovalListResponse(BaseModel):
    approvals: list[ApprovalResponse]


class SellerApprovalDecisionRequest(BaseModel):
    """卖家审批操作使用独立幂等键，意见仅作为审批事实保存。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    comment: str | None = Field(default=None, max_length=2000)

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class SellerApprovalOfferResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    proposer: OfferProposer
    price: Decimal
    shipping_paid_by: ShippingPayer
    shipping_cost: Decimal | None
    seller_borne_discount: Decimal
    additional_terms: dict[str, JsonValue]
    status: OfferStatus
    expires_at: datetime | None
    created_at: datetime


class SellerApprovalResponse(ApprovalResponse):
    product_id: int
    product_title: str
    session_status: NegotiationStatus
    current_offer_id: int | None
    offer: SellerApprovalOfferResponse


class SellerApprovalListResponse(BaseModel):
    approvals: list[SellerApprovalResponse]
