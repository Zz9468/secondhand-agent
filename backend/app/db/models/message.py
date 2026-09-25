from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import MessageRole, stored_enum

if TYPE_CHECKING:
    from app.db.models.negotiation import NegotiationSession


class Message(Base):
    """已经正式发送或接收的会话消息。"""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "request_id",
            name="uq_messages_session_request",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("negotiation_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[MessageRole] = mapped_column(
        stored_enum(MessageRole, name="message_role"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 买家消息保存完整请求指纹；Agent 消息保存可重放的业务结果。
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    formal_offer_id: Mapped[int | None] = mapped_column(
        ForeignKey("offers.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )

    session: Mapped["NegotiationSession"] = relationship(back_populates="messages")
