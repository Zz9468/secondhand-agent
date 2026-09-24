from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentToolContext:
    """由后端身份校验后绑定，字段不会暴露给模型填写。"""

    session_id: int
    buyer_id: str
