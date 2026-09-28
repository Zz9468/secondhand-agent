"""增加 V3 有界模型重试与人工恢复字段。

Revision ID: 20260928_0008
Revises: 20260928_0007
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0008"
down_revision: str | None = "20260928_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FOLLOWUP_CHECK = "ck_approval_requests_approval_followup_status"


def upgrade() -> None:
    op.add_column(
        "model_execution_tasks",
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("3"),
            nullable=False,
        ),
    )
    op.add_column(
        "model_execution_tasks",
        sa.Column(
            "manual_retry_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "model_execution_tasks",
        sa.Column("last_manual_action", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "model_execution_tasks",
        sa.Column("last_manual_actor_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "model_execution_tasks",
        sa.Column("last_manual_reason", sa.String(length=300), nullable=True),
    )
    op.add_column(
        "model_execution_tasks",
        sa.Column("last_manual_at", sa.DateTime(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_model_execution_tasks_max_attempts_positive"),
        "model_execution_tasks",
        "max_attempts > 0",
    )
    op.create_check_constraint(
        op.f("ck_model_execution_tasks_manual_retry_count_nonnegative"),
        "model_execution_tasks",
        "manual_retry_count >= 0",
    )

    op.drop_constraint(op.f(FOLLOWUP_CHECK), "approval_requests", type_="check")
    op.alter_column(
        "approval_requests",
        "followup_status",
        existing_type=sa.String(length=7),
        type_=sa.String(length=15),
        existing_nullable=True,
    )
    op.create_check_constraint(
        op.f(FOLLOWUP_CHECK),
        "approval_requests",
        "followup_status IN ('PENDING', 'SENT', 'FAILED', 'MANUAL_REQUIRED')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE approval_requests SET followup_status = 'FAILED' "
        "WHERE followup_status = 'MANUAL_REQUIRED'"
    )
    op.drop_constraint(op.f(FOLLOWUP_CHECK), "approval_requests", type_="check")
    op.alter_column(
        "approval_requests",
        "followup_status",
        existing_type=sa.String(length=15),
        type_=sa.String(length=7),
        existing_nullable=True,
    )
    op.create_check_constraint(
        op.f(FOLLOWUP_CHECK),
        "approval_requests",
        "followup_status IN ('PENDING', 'SENT', 'FAILED')",
    )

    op.drop_constraint(
        op.f("ck_model_execution_tasks_manual_retry_count_nonnegative"),
        "model_execution_tasks",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_model_execution_tasks_max_attempts_positive"),
        "model_execution_tasks",
        type_="check",
    )
    op.drop_column("model_execution_tasks", "last_manual_at")
    op.drop_column("model_execution_tasks", "last_manual_reason")
    op.drop_column("model_execution_tasks", "last_manual_actor_id")
    op.drop_column("model_execution_tasks", "last_manual_action")
    op.drop_column("model_execution_tasks", "manual_retry_count")
    op.drop_column("model_execution_tasks", "max_attempts")
