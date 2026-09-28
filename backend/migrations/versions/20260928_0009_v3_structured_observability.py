"""增加 V3 结构化观测、关联标识与成本快照。

Revision ID: 20260928_0009
Revises: 20260928_0008
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0009"
down_revision: str | None = "20260928_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_execution_tasks",
        sa.Column("correlation_id", sa.String(length=36), nullable=True),
    )
    op.execute(
        "UPDATE model_execution_tasks SET correlation_id = UUID() "
        "WHERE correlation_id IS NULL"
    )
    op.alter_column(
        "model_execution_tasks",
        "correlation_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.create_index(
        "ix_model_execution_tasks_correlation_id",
        "model_execution_tasks",
        ["correlation_id"],
    )

    usage_columns = (
        sa.Column("cached_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column(
            "input_price_per_million",
            sa.Numeric(18, 8),
            nullable=True,
        ),
        sa.Column(
            "output_price_per_million",
            sa.Numeric(18, 8),
            nullable=True,
        ),
        sa.Column(
            "cached_input_price_per_million",
            sa.Numeric(18, 8),
            nullable=True,
        ),
        sa.Column("cost_currency", sa.String(length=3), nullable=True),
    )
    for column in usage_columns:
        op.add_column("model_execution_tasks", column)

    checks = (
        (
            "cached_input_tokens_nonnegative",
            "cached_input_tokens IS NULL OR cached_input_tokens >= 0",
        ),
        (
            "input_price_per_million_nonnegative",
            "input_price_per_million IS NULL OR input_price_per_million >= 0",
        ),
        (
            "output_price_per_million_nonnegative",
            "output_price_per_million IS NULL OR output_price_per_million >= 0",
        ),
        (
            "cached_input_price_per_million_nonnegative",
            "cached_input_price_per_million IS NULL "
            "OR cached_input_price_per_million >= 0",
        ),
    )
    for name, condition in checks:
        op.create_check_constraint(
            op.f(f"ck_model_execution_tasks_{name}"),
            "model_execution_tasks",
            condition,
        )

    op.create_table(
        "observability_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("correlation_id", sa.String(length=36), nullable=False),
        sa.Column("http_request_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.BigInteger(), nullable=True),
        sa.Column("model_task_id", sa.BigInteger(), nullable=True),
        sa.Column("approval_id", sa.BigInteger(), nullable=True),
        sa.Column("offer_id", sa.BigInteger(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("error_category", sa.String(length=32), nullable=True),
        sa.Column("model_provider", sa.String(length=50), nullable=True),
        sa.Column("model_name", sa.String(length=100), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cached_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("input_price_per_million", sa.Numeric(18, 8), nullable=True),
        sa.Column("output_price_per_million", sa.Numeric(18, 8), nullable=True),
        sa.Column(
            "cached_input_price_per_million",
            sa.Numeric(18, 8),
            nullable=True,
        ),
        sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=True),
        sa.Column("cost_currency", sa.String(length=3), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approval_requests.id"],
            name=op.f("fk_observability_events_approval_id_approval_requests"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["model_task_id"],
            ["model_execution_tasks.id"],
            name=op.f(
                "fk_observability_events_model_task_id_model_execution_tasks"
            ),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["offers.id"],
            name=op.f("fk_observability_events_offer_id_offers"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["negotiation_sessions.id"],
            name=op.f(
                "fk_observability_events_session_id_negotiation_sessions"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_observability_events")),
        sa.UniqueConstraint(
            "event_id",
            name=op.f("uq_observability_events_event_id"),
        ),
    )
    op.create_index(
        "ix_observability_events_correlation_id",
        "observability_events",
        ["correlation_id", "id"],
    )
    op.create_index(
        "ix_observability_events_session_id",
        "observability_events",
        ["session_id", "id"],
    )
    op.create_index(
        "ix_observability_events_model_task_id",
        "observability_events",
        ["model_task_id", "id"],
    )
    op.create_index(
        "ix_observability_events_type",
        "observability_events",
        ["event_type", "id"],
    )


def downgrade() -> None:
    op.drop_table("observability_events")

    for name in (
        "cached_input_price_per_million_nonnegative",
        "output_price_per_million_nonnegative",
        "input_price_per_million_nonnegative",
        "cached_input_tokens_nonnegative",
    ):
        op.drop_constraint(
            op.f(f"ck_model_execution_tasks_{name}"),
            "model_execution_tasks",
            type_="check",
        )
    for column in (
        "cost_currency",
        "cached_input_price_per_million",
        "output_price_per_million",
        "input_price_per_million",
        "cached_input_tokens",
    ):
        op.drop_column("model_execution_tasks", column)
    op.drop_index(
        "ix_model_execution_tasks_correlation_id",
        table_name="model_execution_tasks",
    )
    op.drop_column("model_execution_tasks", "correlation_id")
