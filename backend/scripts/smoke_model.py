"""显式调用真实模型，验证千问结构化输出和基础咨询决策。"""

from app.agent.decision import NegotiationAction
from app.agent.decision_provider import DecisionRequest, LangChainDecisionProvider
from app.agent.model_factory import QwenChatModelFactory
from app.core.config import get_settings


def main() -> None:
    settings = get_settings()
    if not settings.model_is_configured:
        raise SystemExit("请先在本地 .env 配置 MODEL_BASE_URL 和 MODEL_API_KEY")

    provider = LangChainDecisionProvider(QwenChatModelFactory().create(settings))
    decision = provider.decide(
        DecisionRequest(
            buyer_message="请介绍一下商品目前的公开信息。",
            product_context={
                "ok": True,
                "product": {
                    "id": 1001,
                    "title": "模型冒烟测试商品",
                    "description": "仅用于验证结构化输出，不执行正式报价。",
                    "listed_price": "3000.00",
                    "status": "AVAILABLE",
                },
            },
            negotiation_context={
                "ok": True,
                "negotiation": {
                    "id": 1001,
                    "status": "ACTIVE",
                    "current_offer_id": None,
                    "round_count": 0,
                    "max_rounds": 6,
                    "recent_offers": [],
                },
            },
        )
    )
    if decision.action is not NegotiationAction.INQUIRY:
        raise SystemExit(
            f"模型结构化输出有效，但商品咨询动作错误：{decision.action.value}"
        )
    print(decision.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
