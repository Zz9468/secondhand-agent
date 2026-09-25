import pytest

from app.agent.reply_policy import (
    FormalReplyRenderer,
    ReplySafetyError,
)


def test_formal_reply_uses_only_persisted_offer_snapshot() -> None:
    renderer = FormalReplyRenderer()
    tool_result = {
        "ok": True,
        "offer": {
            "id": 42,
            "proposer": "AGENT",
            "price": "2850.00",
            "shipping_paid_by": "buyer",
            "seller_borne_discount": "10.00",
            "additional_terms": {"delivery_method": "pickup"},
            "status": "PROPOSED",
        },
    }

    reply = renderer.render_counter_offer(tool_result)

    assert "2850.00 元" in reply
    assert "不包邮" in reply
    assert "卖家承担优惠 10.00 元" in reply
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
