from sqlalchemy import JSON, BigInteger, ForeignKey, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AgentSessionContext(TimestampMixin, Base):
    """Rebuildable summary/memory cache, independently versioned from business state."""

    __tablename__ = "agent_session_contexts"

    session_id: Mapped[int] = mapped_column(
        ForeignKey("negotiation_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    next_stream_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default=text("1")
    )
    context_version: Mapped[int] = mapped_column(
        nullable=False, default=1, server_default=text("1")
    )
    summary_artifact_id: Mapped[int | None] = mapped_column(BigInteger)
    summary_covered_through_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    summary_text: Mapped[str | None] = mapped_column(Text)
    memory_artifact_id: Mapped[int | None] = mapped_column(BigInteger)
    memory_covered_through_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    memory_json: Mapped[dict | None] = mapped_column(JSON)
    memory_schema_version: Mapped[int] = mapped_column(
        nullable=False, default=1, server_default=text("1")
    )
    legacy_backfill_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default=text("'PENDING'")
    )
