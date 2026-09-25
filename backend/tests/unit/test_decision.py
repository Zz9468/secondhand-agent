import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.agent.decision import NegotiationAction, NegotiationDecision
from app.services.pricing_service import ShippingPayer


def test_counter_requires_price_and_shipping_payer() -> None:
    with pytest.raises(ValidationError):
        NegotiationDecision(
            action=NegotiationAction.COUNTER,
            reason="尝试还价",
            reply="可以再商量。",
        )


def test_accept_requires_existing_offer_id() -> None:
    with pytest.raises(ValidationError):
        NegotiationDecision(
            action=NegotiationAction.ACCEPT,
            reason="接受报价",
            reply="可以接受。",
        )


def test_non_counter_action_cannot_smuggle_price_terms() -> None:
    with pytest.raises(ValidationError):
        NegotiationDecision(
            action=NegotiationAction.INQUIRY,
            proposed_price=Decimal("1000.00"),
            shipping_paid_by=ShippingPayer.SELLER,
            reason="伪装成咨询",
            reply="1000 元包邮。",
        )


def test_valid_counter_decision_is_normalized() -> None:
    decision = NegotiationDecision(
        action=NegotiationAction.COUNTER,
        proposed_price=Decimal("2850"),
        shipping_paid_by=ShippingPayer.BUYER,
        reason="处于自动授权范围",
        reply="候选回复",
    )

    assert decision.proposed_price == Decimal("2850")
    assert decision.additional_terms == {}


def test_money_schema_is_qwen_compatible_and_keeps_decimal_validation() -> None:
    """模型 Schema 不含千问不支持的正则，运行时仍使用 Decimal。"""

    schema_text = json.dumps(NegotiationDecision.model_json_schema())
    assert "(?=" not in schema_text
    assert "(?!" not in schema_text

    decision = NegotiationDecision.model_validate(
        {
            "action": "COUNTER",
            "proposed_price": "2850.25",
            "shipping_paid_by": "buyer",
            "reason": "处于自动授权范围",
            "reply": "候选回复",
        }
    )
    assert decision.proposed_price == Decimal("2850.25")
    assert isinstance(decision.proposed_price, Decimal)

    with pytest.raises(ValidationError):
        NegotiationDecision.model_validate(
            {
                "action": "COUNTER",
                "proposed_price": "2850.001",
                "shipping_paid_by": "buyer",
                "reason": "小数位超限",
                "reply": "候选回复",
            }
        )
