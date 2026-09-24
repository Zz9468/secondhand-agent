from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel

from app.core.config import Settings


class ModelCapabilities(BaseModel):
    """模型参与协商决策前必须具备的能力。"""

    tool_calling: bool
    structured_output: bool


def require_negotiation_capabilities(capabilities: ModelCapabilities) -> None:
    missing: list[str] = []
    if not capabilities.tool_calling:
        missing.append("tool_calling")
    if not capabilities.structured_output:
        missing.append("structured_output")
    if missing:
        raise ValueError(f"model is missing required capabilities: {', '.join(missing)}")


class ChatModelFactory(Protocol):
    """与服务商无关的模型工厂协议，在接入真实模型时实现。"""

    def create(self, settings: Settings) -> BaseChatModel:
        """创建已配置的 LangChain 聊天模型，同时避免暴露访问凭据。"""
        ...
