from decimal import Decimal

from langchain_core.tools import BaseTool, tool
from pydantic import JsonValue

from app.agent.tools.context import AgentToolContext
from app.agent.tools.schemas import AcceptOfferInput, OfferTermsInput
from app.agent.tools.serialization import (
    authorization_result,
    error_result,
    negotiation_state_result,
    offer_result,
)
from app.services.errors import ServiceError
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import PricingError, ShippingPayer


def build_negotiation_tools(
    *,
    context: AgentToolContext,
    negotiation_service: NegotiationService,
) -> list[BaseTool]:
    """创建绑定当前买家会话的协商查询与变更工具。"""

    @tool("get_negotiation_state")
    def get_negotiation_state() -> dict[str, object]:
        """查询当前协商状态、最近报价和非敏感策略信息。"""

        try:
            state = negotiation_service.get_state(
                session_id=context.session_id,
                buyer_id=context.buyer_id,
            )
        except ServiceError as exc:
            return error_result(exc)
        return negotiation_state_result(state)

    @tool("evaluate_offer", args_schema=OfferTermsInput)
    def evaluate_offer_tool(
        price: Decimal,
        shipping_paid_by: ShippingPayer,
        shipping_cost: Decimal | None = None,
        seller_borne_discount: Decimal = Decimal("0.00"),
        additional_terms: dict[str, JsonValue] | None = None,
    ) -> dict[str, object]:
        """评估交易条件的可执行权限，不返回卖家的具体价格阈值。"""

        try:
            payload = OfferTermsInput(
                price=price,
                shipping_paid_by=shipping_paid_by,
                shipping_cost=shipping_cost,
                seller_borne_discount=seller_borne_discount,
                additional_terms=additional_terms or {},
            )
            authorization = negotiation_service.evaluate_offer(
                session_id=context.session_id,
                buyer_id=context.buyer_id,
                terms=payload.to_domain(),
            )
        except (ServiceError, PricingError) as exc:
            return error_result(exc)
        return authorization_result(authorization)

    @tool("submit_counter_offer", args_schema=OfferTermsInput)
    def submit_counter_offer_tool(
        price: Decimal,
        shipping_paid_by: ShippingPayer,
        shipping_cost: Decimal | None = None,
        seller_borne_discount: Decimal = Decimal("0.00"),
        additional_terms: dict[str, JsonValue] | None = None,
    ) -> dict[str, object]:
        """校验并持久化 Agent 正式还价；审批区和禁止区不会写入。"""

        try:
            payload = OfferTermsInput(
                price=price,
                shipping_paid_by=shipping_paid_by,
                shipping_cost=shipping_cost,
                seller_borne_discount=seller_borne_discount,
                additional_terms=additional_terms or {},
            )
            offer = negotiation_service.submit_counter_offer(
                session_id=context.session_id,
                buyer_id=context.buyer_id,
                terms=payload.to_domain(),
                additional_terms=payload.additional_terms,
            )
        except (ServiceError, PricingError) as exc:
            return error_result(exc)
        return offer_result(offer)

    @tool("accept_offer", args_schema=AcceptOfferInput)
    def accept_offer_tool(offer_id: int) -> dict[str, object]:
        """重新读取数据库规则，并仅接受当前自动授权的买家报价。"""

        try:
            offer = negotiation_service.accept_offer(
                session_id=context.session_id,
                buyer_id=context.buyer_id,
                offer_id=offer_id,
            )
        except ServiceError as exc:
            return error_result(exc)
        return offer_result(offer)

    return [
        get_negotiation_state,
        evaluate_offer_tool,
        submit_counter_offer_tool,
        accept_offer_tool,
    ]
