"""P0 Qwen capability contract; never directly authorizes a business action."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["ASK_PRODUCT", "PROPOSE_OFFER", "NONE"]
    amount: str | None
    delivery: Literal["pickup", "shipping"] | None
    source_quote: str


class ProbeBudget:
    def __init__(self):
        self.calls = 0
        self.reserved = Decimal("0")
        self.tokens = 0

    def reserve(self, payload):
        # UTF-8 bytes are a conservative token ceiling for this short text-only probe.
        # Rates are 10x the documented Beijing <=32k tariff; unknown calls keep reserve.
        input_bound = len(payload.encode("utf-8")) + 512
        output_bound = 512
        amount = (Decimal(input_bound) * 2 + Decimal(output_bound) * 8) / 1_000_000
        if self.calls >= 6 or self.reserved + amount > Decimal("0.10"):
            raise RuntimeError("P0 probe budget exhausted")
        if self.tokens + input_bound + output_bound > 20000:
            raise RuntimeError("P0 token budget exhausted")
        self.calls += 1
        self.reserved += amount
        self.tokens += input_bound + output_bound


def parse_proposal(content, source):
    result = Proposal.model_validate_json(content)
    if not result.source_quote or result.source_quote not in source:
        raise ValueError("source quote not present")
    if result.amount is not None:
        amount = Decimal(result.amount)
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
            raise ValueError("invalid proposal money")
    return result
