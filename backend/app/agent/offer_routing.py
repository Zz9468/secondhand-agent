from collections.abc import Mapping

from app.agent.decision import NegotiationAction, NegotiationDecision


def resolve_authorized_offer_decision(
    authorization: Mapping[str, object] | None,
    *,
    current_offer_id: int | None,
) -> NegotiationDecision | None:
    """把可信授权事实转换为无需模型参与的正式报价动作。"""

    if current_offer_id is None or authorization is None:
        return None
    if authorization.get("can_accept_automatically") is True:
        return NegotiationDecision(
            action=NegotiationAction.ACCEPT,
            offer_id=current_offer_id,
            reason="后端规则授权自动接受本轮正式报价",
            reply="由正式回复安全层生成接受结果。",
        )
    if authorization.get("can_request_approval") is True:
        return NegotiationDecision(
            action=NegotiationAction.REQUEST_APPROVAL,
            offer_id=current_offer_id,
            reason="后端规则要求卖家确认本轮正式报价",
            reply="由正式回复安全层生成审批结果。",
        )
    return None


def constrain_formal_offer_decision(
    decision: NegotiationDecision,
    *,
    current_offer_id: int | None,
) -> NegotiationDecision:
    """禁止模型把未获授权的正式报价降级为咨询或越权动作。"""

    if current_offer_id is None or decision.action in {
        NegotiationAction.COUNTER,
        NegotiationAction.REJECT,
    }:
        return decision
    return NegotiationDecision(
        action=NegotiationAction.REJECT,
        reason="本轮正式报价未获得自动接受或卖家审批授权",
        reply="由正式回复安全层生成拒绝结果。",
    )
