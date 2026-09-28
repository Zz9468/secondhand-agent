import pytest

from app.agent.decision import DialogueAct, DialogueActKind, DialogueSubject
from app.agent.reply_policy import (
    DialoguePolicyService,
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
    policy = DialoguePolicyService()
    candidate = "这台 iPhone 15 Pro 配有原装充电线，边框有轻微使用痕迹。"

    reply = policy.resolve(
        acts=[
            DialogueAct(
                kind=DialogueActKind.ASK_FACT,
                subject=DialogueSubject.PRODUCT_DETAILS,
            )
        ],
        candidate_reply=candidate,
        product=PUBLIC_PRODUCT,
    )

    assert reply.text == candidate
    assert reply.needs_clarification is False


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
    policy = DialoguePolicyService()

    reply = policy.resolve(
        acts=[
            DialogueAct(
                kind=DialogueActKind.ASK_FACT,
                subject=DialogueSubject.PRODUCT_DETAILS,
            )
        ],
        candidate_reply=candidate,
        product=PUBLIC_PRODUCT,
    )

    assert reply.text != candidate
    assert "5299.00 元" in reply.text
    assert "4700" not in reply.text
    assert "99%" not in reply.text


def test_sensitive_dialogue_acts_always_use_backend_reply_plan() -> None:
    policy = DialoguePolicyService()

    price_reply = policy.resolve(
        acts=[
            DialogueAct(
                kind=DialogueActKind.ASK_PRIVATE_INFO,
                subject=DialogueSubject.PRICE_FLOOR,
            )
        ],
        candidate_reply="4700 元就是最低价。",
        product=PUBLIC_PRODUCT,
    )
    shipping_reply = policy.resolve(
        acts=[
            DialogueAct(
                kind=DialogueActKind.REQUEST_TERM,
                subject=DialogueSubject.SHIPPING_PAYER,
                requested_value="seller",
            )
        ],
        candidate_reply="可以包邮并保证今天发货。",
        product=PUBLIC_PRODUCT,
    )
    availability_reply = policy.resolve(
        acts=[
            DialogueAct(
                kind=DialogueActKind.ASK_FACT,
                subject=DialogueSubject.AVAILABILITY,
            )
        ],
        candidate_reply="商品已经卖掉了。",
        product=PUBLIC_PRODUCT,
    )

    assert "最低接受价格不能直接公开" in price_reply.text
    assert "5299.00 元" in price_reply.text
    assert "4700" not in price_reply.text
    assert "正式报价" in shipping_reply.text
    assert "包邮" not in shipping_reply.text
    assert availability_reply.text == "商品目前仍在上架，可以继续了解或提交正式报价。"


def test_compound_shipping_and_dispatch_request_gets_complete_specific_reply() -> None:
    policy = DialoguePolicyService()

    reply = policy.resolve(
        acts=[
            DialogueAct(
                kind=DialogueActKind.REQUEST_TERM,
                subject=DialogueSubject.SHIPPING_PAYER,
                requested_value="seller",
            ),
            DialogueAct(
                kind=DialogueActKind.REQUEST_COMMITMENT,
                subject=DialogueSubject.DISPATCH_DEADLINE,
                requested_value="today",
            ),
        ],
        candidate_reply="可以包邮并保证今天发货。",
        product=PUBLIC_PRODUCT,
    )

    assert reply.needs_clarification is False
    assert "运费承担方式可以作为正式报价条件提交" in reply.text
    assert "具体发货时间需要卖家确认" in reply.text
    assert "保证今天发货" not in reply.text


def test_missing_transaction_slot_uses_targeted_clarification() -> None:
    policy = DialoguePolicyService()

    reply = policy.resolve(
        acts=[
            DialogueAct(
                kind=DialogueActKind.REQUEST_TERM,
                subject=DialogueSubject.SHIPPING_PAYER,
            )
        ],
        candidate_reply="请补充条件。",
        product=PUBLIC_PRODUCT,
    )

    assert reply.needs_clarification is True
    assert reply.text == "请说明你希望运费由买家还是卖家承担。"
