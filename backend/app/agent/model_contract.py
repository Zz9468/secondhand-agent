from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel

from app.core.config import Settings


class ModelCapabilities(BaseModel):
    """Capabilities required before a model may drive negotiation decisions."""

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
    """Provider-neutral factory implemented when the real model is connected."""

    def create(self, settings: Settings) -> BaseChatModel:
        """Build a configured LangChain chat model without exposing credentials."""
        ...

