"""Seller Agent 可调用的受约束业务工具。"""

from langchain_core.tools import BaseTool

from app.agent.tools.approval_tools import build_approval_tools
from app.agent.tools.context import AgentToolContext
from app.agent.tools.negotiation_tools import build_negotiation_tools
from app.agent.tools.product_tools import build_product_tools
from app.services.approval_service import ApprovalService
from app.services.negotiation_service import NegotiationService
from app.services.product_service import ProductService


def build_seller_tools(
    *,
    context: AgentToolContext,
    product_service: ProductService,
    negotiation_service: NegotiationService,
    approval_service: ApprovalService,
) -> list[BaseTool]:
    """按已验证上下文组装包含审批能力的 V2 Seller Agent 工具集。"""

    return [
        *build_product_tools(context=context, product_service=product_service),
        *build_negotiation_tools(
            context=context,
            negotiation_service=negotiation_service,
        ),
        *build_approval_tools(
            context=context,
            negotiation_service=negotiation_service,
            approval_service=approval_service,
        ),
    ]


__all__ = ["AgentToolContext", "build_seller_tools"]
