from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.agent.model_contract import (
    ModelCapabilities,
    require_negotiation_capabilities,
)
from app.core.config import Settings


class ModelConfigurationError(ValueError):
    """模型服务缺少必要配置或使用了不支持的提供商。"""


class QwenChatModelFactory:
    """通过千问的 OpenAI 兼容接口创建 LangChain 聊天模型。"""

    capabilities = ModelCapabilities(tool_calling=True, structured_output=True)

    def create(self, settings: Settings) -> BaseChatModel:
        require_negotiation_capabilities(self.capabilities)
        if settings.model_provider.lower() != "qwen":
            raise ModelConfigurationError("MODEL_PROVIDER 当前仅支持 qwen")
        if settings.model_api_key is None:
            raise ModelConfigurationError("未配置 MODEL_API_KEY")
        if not settings.model_base_url:
            raise ModelConfigurationError(
                "未配置 MODEL_BASE_URL，请填写 API Key 所属地域的兼容接口地址"
            )

        return ChatOpenAI(
            model=settings.model_name,
            api_key=settings.model_api_key,
            base_url=settings.model_base_url,
            temperature=settings.model_temperature,
            timeout=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
            # 协商决策由后端规则重新校验，不需要长思考；默认关闭可降低超时概率。
            extra_body={"enable_thinking": settings.model_enable_thinking},
        )
