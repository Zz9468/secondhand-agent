"""Seller Agent 可调用的受约束业务工具。"""

from langchain_core.tools import BaseTool

from app.agent.tools.context import AgentToolContext
from app.agent.tools.negotiation_tools import build_negotiation_tools
from app.agent.tools.product_tools import build_product_tools
from app.services.negotiation_service import NegotiationService
from app.services.product_service import ProductService


def build_seller_tools(
    *,
    context: AgentToolContext,
    product_service: ProductService,
    negotiation_service: NegotiationService,
) -> list[BaseTool]:
    """按已验证上下文组装阶段四的五个工具。"""

    return [
        *build_product_tools(context=context, product_service=product_service),
        *build_negotiation_tools(
            context=context,
            negotiation_service=negotiation_service,
        ),
    ]


__all__ = ["AgentToolContext", "build_seller_tools"]
