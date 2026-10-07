from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentMessage(Base):
    """Append-only execution ledger; public messages are projections of this stream."""

    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "stream_seq", name="uq_agent_messages_session_seq"),
        UniqueConstraint("session_id", "event_key", name="uq_agent_messages_session_event"),
        Index("ix_agent_messages_task_step", "model_task_id", "run_branch_id", "logical_step_id"),
        Index("ix_agent_messages_public_seq", "session_id", "visibility", "stream_seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("negotiation_sessions.id", ondelete="CASCADE"), nullable=False
    )
    stream_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_execution_tasks.id", ondelete="SET NULL")
    )
    run_branch_id: Mapped[str | None] = mapped_column(String(64))
    lease_attempt: Mapped[int | None]
    logical_step_id: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="INTERNAL")
    event_key: Mapped[str] = mapped_column(String(192), nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(String(192))
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    schema_version: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
