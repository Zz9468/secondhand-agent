from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.enums import (
    ModelTaskErrorCategory,
    ModelTaskStatus,
    ModelTaskType,
    stored_enum,
)


class ModelExecutionTask(TimestampMixin, Base):
    """事务外模型调用使用的不可变输入与可恢复执行状态。"""

    __tablename__ = "model_execution_tasks"
    __table_args__ = (
        UniqueConstraint(
            "task_type",
            "business_key",
            name="uq_model_execution_tasks_type_business_key",
        ),
        UniqueConstraint(
            "lease_token",
            name="uq_model_execution_tasks_lease_token",
        ),
        CheckConstraint(
            "snapshot_schema_version > 0",
            name="snapshot_schema_version_positive",
        ),
        CheckConstraint("session_version > 0", name="session_version_positive"),
        CheckConstraint("policy_version > 0", name="policy_version_positive"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        CheckConstraint(
            "manual_retry_count >= 0",
            name="manual_retry_count_nonnegative",
        ),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="input_tokens_nonnegative",
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="output_tokens_nonnegative",
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="total_tokens_nonnegative",
        ),
        CheckConstraint(
            "estimated_cost IS NULL OR estimated_cost >= 0",
            name="estimated_cost_nonnegative",
        ),
        CheckConstraint(
            "CHAR_LENGTH(input_snapshot_hash) = 64",
            name="input_snapshot_hash_length",
        ),
        CheckConstraint(
            "(status = 'RUNNING' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'RUNNING' AND lease_owner IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="lease_matches_running_status",
        ),
        CheckConstraint(
            "(status = 'RETRY_WAIT' AND next_retry_at IS NOT NULL) OR "
            "(status <> 'RETRY_WAIT' AND next_retry_at IS NULL)",
            name="retry_time_matches_status",
        ),
        Index(
            "ix_model_execution_tasks_status_due",
            "status",
            "next_retry_at",
            "id",
        ),
        Index(
            "ix_model_execution_tasks_session_type",
            "session_id",
            "task_type",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_type: Mapped[ModelTaskType] = mapped_column(
        stored_enum(ModelTaskType, name="model_task_type"),
        nullable=False,
    )
    business_key: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("negotiation_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    offer_id: Mapped[int | None] = mapped_column(
        ForeignKey("offers.id", ondelete="SET NULL"),
        nullable=True,
    )
    approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("approval_requests.id", ondelete="SET NULL"),
        nullable=True,
    )
    snapshot_schema_version: Mapped[int] = mapped_column(nullable=False, default=1)
    session_version: Mapped[int] = mapped_column(nullable=False)
    policy_version: Mapped[int] = mapped_column(nullable=False)
    input_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    input_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ModelTaskStatus] = mapped_column(
        stored_enum(ModelTaskStatus, name="model_task_status"),
        nullable=False,
        default=ModelTaskStatus.PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    max_attempts: Mapped[int] = mapped_column(
        nullable=False,
        default=3,
        server_default=text("3"),
    )
    manual_retry_count: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_category: Mapped[ModelTaskErrorCategory | None] = mapped_column(
        stored_enum(ModelTaskErrorCategory, name="model_task_error_category"),
        nullable=True,
    )
    last_error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8),
        nullable=True,
    )
    result_snapshot: Mapped[dict[str, object] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_manual_action: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_manual_actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_manual_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_manual_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
