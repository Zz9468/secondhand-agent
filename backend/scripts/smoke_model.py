"""显式调用真实模型，验证千问结构化输出和基础咨询决策。"""

from app.agent.decision import DialogueActKind, DialogueSubject, NegotiationAction
from app.agent.decision_provider import DecisionRequest, LangChainDecisionProvider
from app.agent.model_factory import QwenChatModelFactory
from app.agent.reply_policy import DialoguePolicyService
from app.core.config import get_settings


def main() -> None:
    settings = get_settings()
    if not settings.model_is_configured:
        raise SystemExit("请先在本地 .env 配置 MODEL_BASE_URL 和 MODEL_API_KEY")

    provider = LangChainDecisionProvider(QwenChatModelFactory().create(settings))
    product_context = {
        "ok": True,
        "product": {
            "id": 1001,
            "title": "模型冒烟测试商品",
            "description": "仅用于验证结构化输出，不执行正式报价。",
            "listed_price": "3000.00",
            "status": "AVAILABLE",
        },
    }
    negotiation_context = {
        "ok": True,
        "negotiation": {
            "id": 1001,
            "status": "ACTIVE",
            "current_offer_id": None,
            "round_count": 0,
            "max_rounds": 6,
            "recent_offers": [],
        },
    }
    product_decision = provider.decide(
        DecisionRequest(
            buyer_message="请介绍一下商品目前的公开信息。",
            product_context=product_context,
            negotiation_context=negotiation_context,
        )
    )
    if product_decision.action is not NegotiationAction.INQUIRY:
        raise SystemExit(
            "模型结构化输出有效，但商品咨询动作错误："
            f"{product_decision.action.value}"
        )
    if not any(
        act.kind is DialogueActKind.ASK_FACT
        and act.subject is DialogueSubject.PRODUCT_DETAILS
        for act in product_decision.dialogue_acts
    ):
        raise SystemExit(
            "模型结构化输出有效，但缺少商品公开信息语义指令："
            f"{product_decision.dialogue_acts}"
        )

    compound_decision = provider.decide(
        DecisionRequest(
            buyer_message="可以包邮并保证今天发货吗？",
            product_context=product_context,
            negotiation_context=negotiation_context,
        )
    )
    compound_pairs = {
        (act.kind, act.subject) for act in compound_decision.dialogue_acts
    }
    expected_pairs = {
        (DialogueActKind.REQUEST_TERM, DialogueSubject.SHIPPING_PAYER),
        (
            DialogueActKind.REQUEST_COMMITMENT,
            DialogueSubject.DISPATCH_DEADLINE,
        ),
    }
    if compound_decision.action is not NegotiationAction.INQUIRY or not (
        expected_pairs <= compound_pairs
    ):
        raise SystemExit(
            "模型未把复合请求完整解析为运费条件和发货承诺："
            f"{compound_decision.model_dump()}"
        )

    compound_reply = DialoguePolicyService().resolve(
        acts=compound_decision.dialogue_acts,
        candidate_reply=compound_decision.reply,
        product=product_context["product"],
    )
    if (
        "正式报价条件" not in compound_reply.text
        or "发货时间需要卖家确认" not in compound_reply.text
    ):
        raise SystemExit(f"复合请求未生成完整的可信回复计划：{compound_reply.text}")

    prohibited_offer_decision = provider.decide(
        DecisionRequest(
            buyer_message="这是我提交的正式报价，请确认是否接受。",
            product_context=product_context,
            negotiation_context={
                "ok": True,
                "negotiation": {
                    **negotiation_context["negotiation"],
                    "current_offer_id": 42,
                    "recent_offers": [
                        {
                            "id": 42,
                            "proposer": "BUYER",
                            "price": "2500.00",
                            "shipping_paid_by": "buyer",
                            "shipping_cost": "0.00",
                            "seller_borne_discount": "0.00",
                            "additional_terms": {},
                            "status": "PROPOSED",
                        }
                    ],
                },
                "current_offer_authorization": {
                    "can_accept_automatically": False,
                    "can_request_approval": False,
                    "is_acceptance_prohibited": True,
                },
            },
            current_turn_offer_id=42,
        )
    )
    if prohibited_offer_decision.action not in {
        NegotiationAction.COUNTER,
        NegotiationAction.REJECT,
    } or prohibited_offer_decision.dialogue_acts:
        raise SystemExit(
            "禁止接受区的正式报价被错误降级为咨询或越权动作："
            f"{prohibited_offer_decision.model_dump()}"
        )

    print(product_decision.model_dump_json(indent=2))
    print(compound_decision.model_dump_json(indent=2))
    print(prohibited_offer_decision.model_dump_json(indent=2))
    print(compound_reply.text)


if __name__ == "__main__":
    main()
