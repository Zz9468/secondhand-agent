from datetime import datetime, timedelta

from langchain_core.tools import BaseTool, tool

from app.agent.tools.context import AgentToolContext
from app.agent.tools.schemas import RequestApprovalInput
from app.agent.tools.serialization import approval_result, error_result
from app.services.approval_service import ApprovalService
from app.services.errors import ApprovalNotAuthorizedError, ServiceError
from app.services.negotiation_service import NegotiationService, OfferSnapshot

_APPROVAL_LIFETIME = timedelta(hours=24)


def build_approval_tools(
    *,
    context: AgentToolContext,
    negotiation_service: NegotiationService,
    approval_service: ApprovalService,
) -> list[BaseTool]:
    """创建只能作用于本轮当前买家报价的审批工具。"""

    @tool("request_approval", args_schema=RequestApprovalInput)
    def request_approval_tool(offer_id: int, reason: str) -> dict[str, object]:
        """重新校验当前正式报价，并为审批区报价创建唯一待审批记录。"""

        try:
            if context.current_turn_offer_id is None or (
                offer_id != context.current_turn_offer_id
            ):
                raise ApprovalNotAuthorizedError("只能为本轮买家的正式报价申请审批")

            state = negotiation_service.get_state(
                session_id=context.session_id,
                buyer_id=context.buyer_id,
            )
            if state.current_offer_id != offer_id:
                raise ApprovalNotAuthorizedError("只能为当前有效报价申请审批")
            current_offer = _find_offer(state.recent_offers, offer_id)
            if current_offer is None:
                raise ApprovalNotAuthorizedError("当前正式报价不存在")

            expires_at = _approval_expiration(current_offer)
            approval = approval_service.create_request(
                session_id=context.session_id,
                buyer_id=context.buyer_id,
                offer_id=offer_id,
                expected_policy_version=state.policy_version,
                reason=reason,
                expires_at=expires_at,
            )
        except ServiceError as exc:
            return error_result(exc)
        return approval_result(approval)

    return [request_approval_tool]


def _find_offer(
    offers: tuple[OfferSnapshot, ...],
    offer_id: int,
) -> OfferSnapshot | None:
    return next((offer for offer in offers if offer.id == offer_id), None)


def _approval_expiration(offer: OfferSnapshot) -> datetime:
    now = (
        datetime.now(tz=offer.expires_at.tzinfo)
        if offer.expires_at
        else datetime.now()
    )
    default_expiration = now + _APPROVAL_LIFETIME
    if offer.expires_at is None:
        return default_expiration
    return min(default_expiration, offer.expires_at)
