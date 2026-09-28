import pytest

from app.agent.decision import InquiryTopic
from app.agent.reply_policy import (
    ConversationalReplyPolicy,
    FormalReplyRenderer,
    ReplySafetyError,
)

PUBLIC_PRODUCT = {
    "id": 1,
    "title": "iPhone 15 Pro 256GB",
    "description": "电池健康度 93%，边框有轻微使用痕迹，附原装充电线。",
    "listed_price": "5299.00",
    "status": "AVAILABLE",
}


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


def test_low_risk_product_inquiry_can_use_safe_model_reply() -> None:
    policy = ConversationalReplyPolicy()
    candidate = "这台 iPhone 15 Pro 配有原装充电线，边框有轻微使用痕迹。"

    reply = policy.render_inquiry(
        topic=InquiryTopic.PRODUCT_DETAILS,
        candidate_reply=candidate,
        product=PUBLIC_PRODUCT,
    )

    assert reply == candidate


@pytest.mark.parametrize(
    "candidate",
    [
        "卖家底价是 4700 元。",
        "卖家已经同意，可以成交。",
        "可以保证今天发货并包邮。",
        "这台手机的电池健康度是 99%。",
        "电池健康度是 93%，售价也是 93 元。",
        "商品当前状态为 AVAILABLE。",
    ],
)
def test_unsafe_model_reply_falls_back_to_trusted_product_facts(candidate: str) -> None:
    policy = ConversationalReplyPolicy()

    reply = policy.render_inquiry(
        topic=InquiryTopic.PRODUCT_DETAILS,
        candidate_reply=candidate,
        product=PUBLIC_PRODUCT,
    )

    assert reply != candidate
    assert "5299.00 元" in reply
    assert "4700" not in reply
    assert "99%" not in reply


def test_sensitive_inquiry_topics_always_use_backend_templates() -> None:
    policy = ConversationalReplyPolicy()

    price_reply = policy.render_inquiry(
        topic=InquiryTopic.PRICE_PROBE,
        candidate_reply="4700 元就是最低价。",
        product=PUBLIC_PRODUCT,
    )
    shipping_reply = policy.render_inquiry(
        topic=InquiryTopic.SHIPPING,
        candidate_reply="可以包邮并保证今天发货。",
        product=PUBLIC_PRODUCT,
    )
    availability_reply = policy.render_inquiry(
        topic=InquiryTopic.AVAILABILITY,
        candidate_reply="商品已经卖掉了。",
        product=PUBLIC_PRODUCT,
    )

    assert "最低接受价格不能直接公开" in price_reply
    assert "5299.00 元" in price_reply
    assert "4700" not in price_reply
    assert "正式报价" in shipping_reply
    assert "包邮" not in shipping_reply
    assert availability_reply == "商品目前仍在上架，可以继续了解或提交正式报价。"


def test_safe_clarification_uses_model_reply_and_unsafe_one_falls_back() -> None:
    policy = ConversationalReplyPolicy()
    safe_reply = "你更想了解商品成色，还是配件情况？"

    assert policy.render_clarification(
        candidate_reply=safe_reply,
        product=PUBLIC_PRODUCT,
    ) == safe_reply
    assert policy.render_clarification(
        candidate_reply="卖家底价是四千七百元。",
        product=PUBLIC_PRODUCT,
    ) == "请具体说明你想了解的商品信息或交易条件。"
