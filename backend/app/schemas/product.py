from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models import NegotiationStyle, ProductStatus


class PolicyWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_net_price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    auto_accept_threshold: Decimal = Field(
        ge=0,
        max_digits=12,
        decimal_places=2,
    )
    negotiation_style: NegotiationStyle = NegotiationStyle.BALANCED
    max_rounds: int = Field(default=6, ge=1, le=100)

    @model_validator(mode="after")
    def validate_price_order(self) -> Self:
        if self.auto_accept_threshold < self.minimum_net_price:
            raise ValueError("自动接受阈值不能低于最低净收入")
        return self


class ProductCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
    listed_price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    status: ProductStatus = ProductStatus.DRAFT
    policy: PolicyWriteRequest

    @field_validator("title", "description")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("商品标题和描述不能为空")
        return value.strip()


class ProductUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
    listed_price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    status: ProductStatus

    @field_validator("title", "description")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("商品标题和描述不能为空")
        return value.strip()


class PolicyUpdateRequest(PolicyWriteRequest):
    expected_version: int = Field(ge=1)


class PublicProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str
    listed_price: Decimal
    status: ProductStatus


class PublicProductListResponse(BaseModel):
    products: list[PublicProductResponse]


class SellerPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    minimum_net_price: Decimal
    auto_accept_threshold: Decimal
    negotiation_style: NegotiationStyle
    max_rounds: int
    version: int


class SellerProductResponse(PublicProductResponse):
    policy: SellerPolicyResponse


class SellerProductListResponse(BaseModel):
    products: list[SellerProductResponse]
