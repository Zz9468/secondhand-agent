"""Versioned, bounded semantic proposals and public chat contracts."""

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = "negotiation-v2"
QUALITY_VERSION = "negotiation-v3"
SEMANTIC_VERSIONS = {VERSION, QUALITY_VERSION}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ChatInput(StrictModel):
    kind: Literal["CHAT"] = "CHAT"
    text: str = Field(min_length=1, max_length=4000)
    client_view_id: str = Field(min_length=1, max_length=64)
    display_receipt_id: str | None = Field(default=None, max_length=64)
    reply_to_message_id: int | None = Field(default=None, gt=0)
    draft_id: str | None = Field(default=None, max_length=32)
    draft_version: int | None = Field(default=None, gt=0)
    action: Literal["CONFIRM", "CLOSE"] | None = None
    offer_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def paired(self):
        if (self.draft_id is None) != (self.draft_version is None):
            raise ValueError("草案标识和版本须同时提供")
        if self.action == "CONFIRM" and (not self.offer_id or not self.display_receipt_id):
            raise ValueError("确认须引用具体报价和展示回执")
        if self.offer_id is not None and self.action != "CONFIRM":
            raise ValueError("报价标识仅用于确认按钮")
        return self


class ChatRequest(StrictModel):
    request_id: str = Field(min_length=8, max_length=56, pattern=r"^[A-Za-z0-9_-]+$")
    content: str = Field(min_length=1, max_length=4000)
    client_view_id: str = Field(min_length=1, max_length=64)
    display_receipt_id: str | None = Field(default=None, max_length=64)
    reply_to_message_id: int | None = Field(default=None, gt=0)
    draft_id: str | None = Field(default=None, max_length=32)
    draft_version: int | None = Field(default=None, gt=0)

    def as_input(self):
        body = self.model_dump(exclude={"request_id", "content"})
        return ChatInput(text=self.content, **body)


class Predicate(StrictModel):
    fact: Literal["battery_health_pct", "repair_history", "allowed_delivery_methods", "unsupported"]
    operator: Literal[">=", "<=", "==", "contains"]
    value: str = Field(min_length=1, max_length=100)
    source_quote: str = Field(min_length=1, max_length=500)


class Candidate(StrictModel):
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    delivery: Literal["pickup", "shipping"] | None = None
    payer: Literal["buyer", "seller"] | None = None
    region: str | None = Field(default=None, min_length=1, max_length=80)
    conditions: list[Predicate] = Field(default_factory=list, max_length=8)
    commitments: list[str] = Field(default_factory=list, max_length=8)


class Intent(StrictModel):
    id: str = Field(min_length=1, max_length=32)
    type: Literal[
        "ASK_PRODUCT",
        "ASK_TERMS",
        "PROPOSE_OFFER",
        "CHANGE_TERMS",
        "CONFIRM_INTENT",
        "WITHDRAW_OFFER",
        "CLOSE_SESSION",
        "PROVIDE_INFO",
        "OTHER",
    ]
    source_quote: str = Field(min_length=1, max_length=4000)
    topic: str | None = Field(default=None, max_length=80)
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    candidate: Candidate | None = None
    alternative_group: str | None = Field(default=None, max_length=32)


class IntentFrame(StrictModel):
    schema_version: Literal["intent-v1"] = "intent-v1"
    intents: list[Intent] = Field(min_length=1, max_length=8)
    ambiguous: bool = False

    def validated_for(self, source):
        """Reject ungrounded fields and cyclic/deep dependencies before any effects."""
        by_id = {item.id: item for item in self.intents}
        if len(by_id) != len(self.intents):
            raise ValueError("重复意图编号")
        for item in self.intents:
            if item.source_quote not in source:
                raise ValueError("意图引用不属于当前输入")
            if item.type in {"PROPOSE_OFFER", "CHANGE_TERMS"} and (
                item.candidate is None
                or not any(
                    item.candidate.model_dump().get(key)
                    for key in (
                        "amount",
                        "delivery",
                        "payer",
                        "region",
                        "conditions",
                        "commitments",
                    )
                )
            ):
                raise ValueError("报价或修改条款必须包含本轮明确候选，不能复用未修改的历史条款")
            if item.candidate:
                candidate = item.candidate
                numbers = {Decimal(n) for n in re.findall(r"\d+(?:\.\d+)?", source)}
                if candidate.amount is not None and candidate.amount not in numbers:
                    raise ValueError("金额没有当前原文依据")
                for condition in candidate.conditions:
                    if condition.source_quote not in source:
                        raise ValueError("条件没有原文依据")
                if any(value not in source for value in candidate.commitments):
                    raise ValueError("额外承诺没有原文依据")
                if candidate.region and candidate.region not in source:
                    raise ValueError("区域没有原文依据")
                if candidate.delivery == "pickup" and not re.search(r"自提|面交|当面", source):
                    raise ValueError("自提没有当前原文依据")
                if candidate.delivery == "shipping" and not re.search(r"邮|快递|寄|到付", source):
                    raise ValueError("配送方式没有当前原文依据")
                if candidate.payer == "seller" and not re.search(
                    r"包邮|卖家.*运费|你.*运费", source
                ):
                    raise ValueError("卖家承担运费没有原文依据")
                if (
                    candidate.payer == "buyer"
                    and candidate.delivery != "pickup"
                    and not re.search(r"到付|运费我|我.*运费|买家.*运费", source)
                ):
                    raise ValueError("买家承担运费没有原文依据")
        ordered, visiting = [], set()

        def visit(key, depth=0):
            if key not in by_id or key in visiting or depth > 4:
                raise ValueError("依赖无效、成环或超过深度限制")
            if key in ordered:
                return
            visiting.add(key)
            for parent in by_id[key].depends_on:
                visit(parent, depth + 1)
            visiting.remove(key)
            ordered.append(key)

        for key in by_id:
            visit(key)
        return [by_id[key] for key in ordered]
