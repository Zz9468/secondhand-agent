"""P3 approval effects, durable events and independent execution ownership."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "d34a8b9c71f0"
down_revision = "c8a72c323d5c"
branch_labels = depends_on = None


def upgrade():
    op.add_column("negotiation_sessions", sa.Column("lifecycle_checked_at", sa.DateTime()))
    op.create_index(
        "ix_negotiation_sessions_lifecycle_checked_at",
        "negotiation_sessions",
        ["lifecycle_checked_at"],
    )
    for table in ("offers", "approval_requests"):
        op.add_column(table, sa.Column("invalid_reason", sa.String(100), nullable=True))
        op.add_column(table, sa.Column("invalidated_at", sa.DateTime(), nullable=True))
        op.add_column(table, sa.Column("superseded_by", sa.BigInteger(), nullable=True))
    op.add_column("approval_requests", sa.Column("effect_status", sa.String(24), nullable=True))
    op.add_column(
        "approval_requests",
        sa.Column("effect_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("approval_requests", sa.Column("decision_hash", sa.String(64), nullable=True))
    op.add_column(
        "approval_requests", sa.Column("notification_status", sa.String(24), nullable=True)
    )
    for table in ("business_action_receipts", "notification_deliveries"):
        op.alter_column(table, "input_id", existing_type=sa.String(32), nullable=True)
    op.alter_column("outbox_events", "delivery_id", existing_type=sa.String(64), nullable=True)
    for table, column, target in (
        ("notification_deliveries", "session_id", "negotiation_sessions"),
        ("notification_deliveries", "approval_id", "approval_requests"),
        ("outbox_events", "approval_id", "approval_requests"),
    ):
        op.add_column(table, sa.Column(column, sa.BigInteger(), nullable=True))
        op.create_foreign_key(
            op.f(f"fk_{table}_{column}_{target}"), table, target, [column], ["id"]
        )
        op.create_index(op.f(f"ix_{table}_{column}"), table, [column])
    time = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
    op.create_table(
        "approval_executions",
        sa.Column("approval_id", sa.BigInteger(), nullable=False),
        sa.Column("graph_version", sa.String(32), nullable=False),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_until", time),
        sa.Column("ownership_epoch", sa.Integer(), nullable=False),
        sa.Column("checkpoint_ref", sa.String(100)),
        sa.Column("recovery_required", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", time),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approval_requests.id"],
            name=op.f("fk_approval_executions_approval_id_approval_requests"),
        ),
        sa.PrimaryKeyConstraint("approval_id", name=op.f("pk_approval_executions")),
    )
    op.create_index(
        "ix_approval_executions_next_retry_at", "approval_executions", ["next_retry_at"]
    )
    # Enrol existing P1/P2 pending approvals without touching legacy decisions.
    op.execute("""
        UPDATE approval_requests a JOIN negotiation_sessions s ON s.id=a.session_id
        SET a.effect_status='NOT_APPLIED' WHERE s.graph_version IS NOT NULL
    """)
    op.execute("""
        INSERT INTO approval_executions
          (approval_id, graph_version, ownership_epoch, recovery_required, attempts)
        SELECT a.id, 'approval-v1', 0, 0, 0 FROM approval_requests a
        JOIN negotiation_sessions s ON s.id=a.session_id WHERE s.graph_version IS NOT NULL
    """)
    op.execute("""
        INSERT INTO outbox_events (id, approval_id, payload, status)
        SELECT CONCAT('approval', CHAR(58), a.id, CHAR(58), 'requested'), a.id,
          JSON_OBJECT('schema', 1, 'approval_id', a.id, 'kind', 'REQUESTED'), 'PENDING'
        FROM approval_requests a JOIN negotiation_sessions s ON s.id=a.session_id
        WHERE s.graph_version IS NOT NULL
    """)


def downgrade():
    # Never delete P3 audit records to make a downgrade succeed.
    if (
        op.get_bind()
        .execute(sa.text("SELECT id FROM business_action_receipts WHERE input_id IS NULL LIMIT 1"))
        .first()
        or op.get_bind()
        .execute(sa.text("SELECT id FROM notification_deliveries WHERE input_id IS NULL LIMIT 1"))
        .first()
    ):
        raise RuntimeError("P3 lifecycle history exists; archive it before downgrading")
    op.execute("DELETE FROM outbox_events WHERE delivery_id IS NULL")
    op.drop_table("approval_executions")
    for table, column, target in (
        ("outbox_events", "approval_id", "approval_requests"),
        ("notification_deliveries", "approval_id", "approval_requests"),
        ("notification_deliveries", "session_id", "negotiation_sessions"),
    ):
        op.drop_constraint(op.f(f"fk_{table}_{column}_{target}"), table, type_="foreignkey")
        op.drop_index(op.f(f"ix_{table}_{column}"), table)
        op.drop_column(table, column)
    op.alter_column("outbox_events", "delivery_id", existing_type=sa.String(64), nullable=False)
    for table in ("business_action_receipts", "notification_deliveries"):
        op.alter_column(table, "input_id", existing_type=sa.String(32), nullable=False)
    for column in ("notification_status", "decision_hash", "effect_revision", "effect_status"):
        op.drop_column("approval_requests", column)
    for table in ("offers", "approval_requests"):
        for column in ("superseded_by", "invalidated_at", "invalid_reason"):
            op.drop_column(table, column)
    op.drop_index("ix_negotiation_sessions_lifecycle_checked_at", "negotiation_sessions")
    op.drop_column("negotiation_sessions", "lifecycle_checked_at")
