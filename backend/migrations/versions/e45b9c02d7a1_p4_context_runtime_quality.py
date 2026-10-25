"""P4 durable activity budgets, sourced context, recovery audit and archive tombstone."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "e45b9c02d7a1"
down_revision = "d34a8b9c71f0"
branch_labels = depends_on = None


def upgrade():
    time = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
    op.add_column("negotiation_sessions", sa.Column("archived_at", sa.DateTime()))
    op.add_column(
        "negotiation_inputs",
        sa.Column("active_ms", sa.BigInteger(), nullable=False, server_default="0"),
    )
    for name in ("active_since", "active_until", "closing_deadline"):
        op.add_column("negotiation_inputs", sa.Column(name, time))
    for name, kind in (
        ("purpose", sa.String(64)),
        ("model_name", sa.String(100)),
        ("finished_at", time),
        ("error_category", sa.String(64)),
        ("request_snapshot", sa.JSON()),
    ):
        op.add_column("model_call_ledger", sa.Column(name, kind))
    op.create_table(
        "graph_turn_quality",
        sa.Column(
            "input_id", sa.String(32), sa.ForeignKey("negotiation_inputs.id"), primary_key=True
        ),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("evidence_hash", sa.String(64)),
        sa.Column("reply", sa.String(4000)),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("review_count", sa.Integer(), nullable=False),
        sa.Column("stop_reason", sa.String(64)),
        sa.Column("review_feedback", sa.String(500)),
    )
    op.create_table(
        "graph_recovery_actions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "session_id",
            sa.BigInteger(),
            sa.ForeignKey("negotiation_sessions.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("actor_id", sa.String(64), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("target", sa.String(100), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_at", time, nullable=False),
    )


def downgrade():
    bind = op.get_bind()
    if any(bind.scalar(sa.text(query)) for query in (
        "SELECT COUNT(*) FROM graph_turn_quality",
        "SELECT COUNT(*) FROM graph_recovery_actions",
        "SELECT COUNT(*) FROM negotiation_sessions WHERE graph_version='negotiation-v3'",
        "SELECT COUNT(*) FROM negotiation_inputs WHERE active_ms>0 OR active_since IS NOT NULL",
    )):
        raise RuntimeError("P4 已有运行、预算或恢复审计，不能通过降级删除这些证据")
    op.drop_table("graph_recovery_actions")
    op.drop_table("graph_turn_quality")
    for name in ("purpose", "model_name", "finished_at", "error_category", "request_snapshot"):
        op.drop_column("model_call_ledger", name)
    for name in ("active_ms", "active_since", "active_until", "closing_deadline"):
        op.drop_column("negotiation_inputs", name)
    op.drop_column("negotiation_sessions", "archived_at")
