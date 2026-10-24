"""P1 内部类型化输入；不作为公开聊天请求契约。"""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

GRAPH_VERSION = "negotiation-v1"
CONFIG = {"schema": "p1-v1", "max_calls": 10, "max_tokens": 180000}


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


class Conflict(ValueError):
    """公开安全的协议或业务冲突。"""


class StaleOwner(Conflict):
    pass


class BudgetExhausted(Conflict):
    pass


class Terms(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    shipping_paid_by: Literal["buyer", "seller"] = "buyer"
    shipping_cost: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    seller_discount: Decimal | None = Field(
        default=Decimal("0"), ge=0, max_digits=12, decimal_places=2
    )
    additional_terms: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def coherent_delivery(self):
        if self.additional_terms.get("delivery_method") == "pickup" and (
            self.shipping_paid_by == "seller" or self.shipping_cost not in {None, Decimal("0")}
        ):
            raise ValueError("自提不能包含卖家承担运费或非零运费")
        return self


class TypedInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["SUBMIT", "REPLACE", "CONFIRM", "WITHDRAW", "CLOSE", "QUERY"]
    text: str = Field(default="", max_length=4000)
    terms: Terms | None = None
    counter_terms: Terms | None = None
    offer_id: int | None = Field(default=None, gt=0)
    client_view_id: str = Field(default="internal", min_length=1, max_length=64)
    display_receipt_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def check_kind(self):
        if (self.kind in {"SUBMIT", "REPLACE"}) != (self.terms is not None):
            raise ValueError("报价输入必须且仅能携带报价条款")
        if self.counter_terms and self.kind not in {"SUBMIT", "REPLACE"}:
            raise ValueError("只有报价可以携带内部还价候选")
        if self.kind in {"CONFIRM", "WITHDRAW"} and self.offer_id is None:
            raise ValueError("必须引用具体报价")
        return self


def thread_id(session_id):
    return f"negotiation:{session_id}:v1"
