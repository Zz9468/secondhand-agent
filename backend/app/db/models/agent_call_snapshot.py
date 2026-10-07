from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentCallSnapshot(Base):
    __tablename__ = "agent_call_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "model_task_id", "logical_step_id", "call_attempt", name="uq_agent_call_attempt"
        ),
        Index("ix_agent_call_task_status", "model_task_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_task_id: Mapped[int] = mapped_column(
        ForeignKey("model_execution_tasks.id", ondelete="CASCADE"), nullable=False
    )
    run_branch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_step_id: Mapped[str] = mapped_column(String(64), nullable=False)
    call_attempt: Mapped[int] = mapped_column(nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    context_manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_payload: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="STARTED")
    usage_json: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
