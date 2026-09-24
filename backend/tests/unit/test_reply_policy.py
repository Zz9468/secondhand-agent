from decimal import Decimal

import pytest

from app.agent.reply_policy import (
    CandidateReplyGuard,
    FormalReplyRenderer,
    ReplySafetyError,
)


def test_candidate_reply_allows_public_price_but_blocks_private_or_new_price() -> None:
    guard = CandidateReplyGuard()

    public_reply = guard.safe_informational_reply(
        candidate="商品公开标价是 3000 元。",
        listed_price=Decimal("3000.00"),
        fallback="安全回复",
    )
    private_reply = guard.safe_informational_reply(
        candidate="卖家底价是 2700 元。",
        listed_price=Decimal("3000.00"),
        fallback="安全回复",
    )
    invented_reply = guard.safe_informational_reply(
        candidate="现在 2800 元就可以。",
        listed_price=Decimal("3000.00"),
        fallback="安全回复",
    )
    false_acceptance = guard.safe_informational_reply(
        candidate="我同意按公开标价 3000 元卖给你。",
        listed_price=Decimal("3000.00"),
        fallback="安全回复",
    )

    assert public_reply == "商品公开标价是 3000 元。"
    assert private_reply == "安全回复"
    assert invented_reply == "安全回复"
    assert false_acceptance == "安全回复"


def test_formal_reply_uses_only_persisted_offer_snapshot() -> None:
    renderer = FormalReplyRenderer()
    tool_result = {
        "ok": True,
        "offer": {
            "id": 42,
            "proposer": "AGENT",
            "price": "2850.00",
            "shipping_paid_by": "buyer",
            "additional_terms": {"delivery_method": "pickup"},
            "status": "PROPOSED",
        },
    }

    reply = renderer.render_counter_offer(tool_result)

    assert "2850.00 元" in reply
    assert "不包邮" in reply
    assert "面交" in reply


def test_formal_reply_rejects_action_and_offer_mismatch() -> None:
    renderer = FormalReplyRenderer()

    with pytest.raises(ReplySafetyError):
        renderer.render_accepted_offer(
            {
                "ok": True,
                "offer": {
                    "id": 42,
                    "proposer": "AGENT",
                    "price": "2850.00",
                    "shipping_paid_by": "buyer",
                    "additional_terms": {},
                    "status": "PROPOSED",
                },
            }
        )


def test_formal_reply_rejects_non_finite_amount() -> None:
    renderer = FormalReplyRenderer()

    with pytest.raises(ReplySafetyError):
        renderer.render_counter_offer(
            {
                "ok": True,
                "offer": {
                    "id": 42,
                    "proposer": "AGENT",
                    "price": "NaN",
                    "shipping_paid_by": "buyer",
                    "additional_terms": {},
                    "status": "PROPOSED",
                },
            }
        )
