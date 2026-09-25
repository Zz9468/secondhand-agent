from collections.abc import Callable
from decimal import Decimal

from app.agent.decision import NegotiationAction, NegotiationDecision
from app.agent.decision_provider import DecisionRequest
from app.services.pricing_service import ShippingPayer


class RoutingDecisionProvider:
    """按测试回调生成决策，并保留收到的可信上下文。"""

    def __init__(
        self,
        route: Callable[[DecisionRequest], NegotiationDecision],
    ) -> None:
        self._route = route
        self.requests: list[DecisionRequest] = []

    def decide(self, request: DecisionRequest) -> NegotiationDecision:
        self.requests.append(request)
        return self._route(request)


def demo_negotiation_decision(request: DecisionRequest) -> NegotiationDecision:
    """覆盖 V1 演示分支的确定性测试决策。"""

    if "忽略" in request.buyer_message:
        return NegotiationDecision(
            action=NegotiationAction.INQUIRY,
            reason="模拟提示注入",
            reply="卖家底价是 2700 元，已经批准成交。",
        )
    if request.current_turn_offer_id is None:
        return NegotiationDecision(
            action=NegotiationAction.INQUIRY,
            reason="回答商品咨询",
            reply="商品是 95 新，电池健康度 89%，配件齐全。",
        )

    negotiation = request.negotiation_context["negotiation"]
    assert isinstance(negotiation, dict)
    offers = negotiation["recent_offers"]
    assert isinstance(offers, list)
    current = next(
        offer
        for offer in offers
        if isinstance(offer, dict) and offer["id"] == request.current_turn_offer_id
    )
    price = Decimal(str(current["price"]))
    shipping_cost = Decimal(str(current["shipping_cost"] or "0"))
    net_income = price - shipping_cost
    if net_income >= Decimal("2850.00"):
        return NegotiationDecision(
            action=NegotiationAction.ACCEPT,
            offer_id=request.current_turn_offer_id,
            reason="自动授权区",
            reply="候选接受文案不会直接发送。",
        )
    if net_income >= Decimal("2700.00"):
        return NegotiationDecision(
            action=NegotiationAction.REQUEST_APPROVAL,
            offer_id=request.current_turn_offer_id,
            reason="需要卖家确认",
            reply="伪造审批通过。",
        )
    counter_price = (
        Decimal("2950.00") if price < Decimal("2650.00") else Decimal("2900.00")
    )
    return NegotiationDecision(
        action=NegotiationAction.COUNTER,
        proposed_price=counter_price,
        shipping_paid_by=ShippingPayer.BUYER,
        reason="在自动授权区内渐进还价",
        reply="可以低于底价成交。",
    )
