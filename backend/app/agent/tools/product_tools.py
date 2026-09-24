from langchain_core.tools import BaseTool, tool

from app.agent.tools.context import AgentToolContext
from app.agent.tools.serialization import error_result, product_result
from app.services.errors import ServiceError
from app.services.product_service import ProductService


def build_product_tools(
    *,
    context: AgentToolContext,
    product_service: ProductService,
) -> list[BaseTool]:
    """创建仅能访问当前已授权会话商品的只读工具。"""

    @tool("get_product_info")
    def get_product_info() -> dict[str, object]:
        """查询当前协商商品可向买家公开的真实信息。"""

        try:
            product = product_service.get_for_negotiation(
                session_id=context.session_id,
                buyer_id=context.buyer_id,
            )
        except ServiceError as exc:
            return error_result(exc)
        return product_result(product)

    return [get_product_info]
