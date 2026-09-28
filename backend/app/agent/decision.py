from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from app.services.pricing_service import ShippingPayer

# Pydantic 默认会为 Decimal 生成带前瞻语法的字符串正则，千问的 JSON
# Schema 解析器暂不支持该语法。这里只简化发给模型的 Schema；运行时仍由
# Decimal、max_digits 和 decimal_places 完成精确校验。
DecisionMoney = Annotated[
    Decimal,
    Field(ge=0, max_digits=12, decimal_places=2),
    WithJsonSchema(
        {
            "type": "number",
            "minimum": 0,
            "maximum": 9999999999.99,
        }
    ),
]


class NegotiationAction(StrEnum):
    INQUIRY = "INQUIRY"
    ACCEPT = "ACCEPT"
    COUNTER = "COUNTER"
    REJECT = "REJECT"
    REQUEST_APPROVAL = "REQUEST_APPROVAL"


class DialogueActKind(StrEnum):
    """买家话语在业务层面的作用，不包含任何执行授权。"""

    ASK_FACT = "ASK_FACT"
    ASK_PRIVATE_INFO = "ASK_PRIVATE_INFO"
    REQUEST_TERM = "REQUEST_TERM"
    REQUEST_COMMITMENT = "REQUEST_COMMITMENT"
    GENERAL = "GENERAL"
    UNKNOWN = "UNKNOWN"


class DialogueSubject(StrEnum):
    """Seller Agent 当前能够识别并由后端裁决的业务概念。"""

    PRODUCT_DETAILS = "PRODUCT_DETAILS"
    LISTED_PRICE = "LISTED_PRICE"
    PRICE_FLOOR = "PRICE_FLOOR"
    OFFER_PRICE = "OFFER_PRICE"
    AVAILABILITY = "AVAILABILITY"
    SHIPPING_PAYER = "SHIPPING_PAYER"
    SHIPPING_COST = "SHIPPING_COST"
    DELIVERY_METHOD = "DELIVERY_METHOD"
    DISPATCH_DEADLINE = "DISPATCH_DEADLINE"
    RESERVATION = "RESERVATION"
    GENERAL = "GENERAL"
    OTHER = "OTHER"


class DialogueAct(BaseModel):
    """模型解析出的单项语义指令；具体措辞不会参与权限判断。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: DialogueActKind
    subject: DialogueSubject
    requested_value: str | None = Field(default=None, max_length=120)

    @field_validator("requested_value", mode="before")
    @classmethod
    def normalize_missing_value(cls, value: object) -> object:
        """兼容部分模型用空字符串表达 JSON Schema 中的可空字段。"""

        if isinstance(value, str) and not value.strip():
            return None
        return value


class NegotiationDecision(BaseModel):
    """模型内部决策契约，不能直接作为买家响应返回。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: NegotiationAction
    dialogue_acts: list[DialogueAct] = Field(default_factory=list, max_length=8)
    offer_id: int | None = Field(default=None, gt=0)
    proposed_price: DecisionMoney | None = None
    shipping_paid_by: ShippingPayer | None = None
    shipping_cost: DecisionMoney | None = None
    seller_borne_discount: DecisionMoney | None = None
    additional_terms: dict[str, JsonValue] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=500)
    reply: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def validate_action_fields(self) -> Self:
        if self.action is NegotiationAction.INQUIRY:
            if not self.dialogue_acts:
                raise ValueError("INQUIRY requires at least one dialogue act")
        elif self.dialogue_acts:
            raise ValueError(f"{self.action.value} cannot include dialogue acts")

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
