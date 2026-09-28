"""增加 V3 持久化模型执行任务。

Revision ID: 20260928_0007
Revises: 20260927_0006
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0007"
down_revision: str | None = "20260927_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

model_task_type = sa.Enum(
    "CHAT_DECISION",
    "APPROVAL_FOLLOWUP",
    name="model_task_type",
    native_enum=False,
    create_constraint=True,
    length=17,
)
model_task_status = sa.Enum(
    "PENDING",
    "RUNNING",
    "RETRY_WAIT",
    "SUCCEEDED",
    "FAILED",
    "STALE",
    "CANCELLED",
    name="model_task_status",
    native_enum=False,
    create_constraint=True,
    length=10,
)
model_task_error_category = sa.Enum(
    "MODEL_TIMEOUT",
    "RATE_LIMITED",
    "NETWORK",
    "INVALID_OUTPUT",
    "BUSINESS_CONFLICT",
    "INTERNAL",
    "UNKNOWN",
    name="model_task_error_category",
    native_enum=False,
    create_constraint=True,
    length=17,
)


def upgrade() -> None:
    op.create_table(
        "model_execution_tasks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_type", model_task_type, nullable=False),
        sa.Column("business_key", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("offer_id", sa.BigInteger(), nullable=True),
        sa.Column("approval_id", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_schema_version", sa.Integer(), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("input_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("status", model_task_status, nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "last_error_category",
            model_task_error_category,
            nullable=True,
        ),
        sa.Column("last_error_message", sa.String(length=500), nullable=True),
        sa.Column("model_provider", sa.String(length=50), nullable=True),
        sa.Column("model_name", sa.String(length=100), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=True),
        sa.Column("result_snapshot", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "snapshot_schema_version > 0",
            name=op.f(
                "ck_model_execution_tasks_snapshot_schema_version_positive"
            ),
        ),
        sa.CheckConstraint(
            "session_version > 0",
            name=op.f("ck_model_execution_tasks_session_version_positive"),
        ),
        sa.CheckConstraint(
            "policy_version > 0",
            name=op.f("ck_model_execution_tasks_policy_version_positive"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_model_execution_tasks_attempt_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name=op.f("ck_model_execution_tasks_input_tokens_nonnegative"),
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name=op.f("ck_model_execution_tasks_output_tokens_nonnegative"),
        ),
        sa.CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name=op.f("ck_model_execution_tasks_total_tokens_nonnegative"),
        ),
        sa.CheckConstraint(
            "estimated_cost IS NULL OR estimated_cost >= 0",
            name=op.f("ck_model_execution_tasks_estimated_cost_nonnegative"),
        ),
        sa.CheckConstraint(
            "CHAR_LENGTH(input_snapshot_hash) = 64",
            name=op.f("ck_model_execution_tasks_input_snapshot_hash_length"),
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'RUNNING' AND lease_owner IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_model_execution_tasks_lease_matches_running_status"),
        ),
        sa.CheckConstraint(
            "(status = 'RETRY_WAIT' AND next_retry_at IS NOT NULL) OR "
            "(status <> 'RETRY_WAIT' AND next_retry_at IS NULL)",
            name=op.f("ck_model_execution_tasks_retry_time_matches_status"),
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approval_requests.id"],
            name=op.f(
                "fk_model_execution_tasks_approval_id_approval_requests"
            ),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["offers.id"],
            name=op.f("fk_model_execution_tasks_offer_id_offers"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["negotiation_sessions.id"],
            name=op.f(
                "fk_model_execution_tasks_session_id_negotiation_sessions"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_execution_tasks")),
        sa.UniqueConstraint(
            "lease_token",
            name=op.f("uq_model_execution_tasks_lease_token"),
        ),
        sa.UniqueConstraint(
            "task_type",
            "business_key",
            name=op.f("uq_model_execution_tasks_type_business_key"),
        ),
    )
    op.create_index(
        "ix_model_execution_tasks_session_type",
        "model_execution_tasks",
        ["session_id", "task_type"],
    )
    op.create_index(
        "ix_model_execution_tasks_status_due",
        "model_execution_tasks",
        ["status", "next_retry_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("model_execution_tasks")
