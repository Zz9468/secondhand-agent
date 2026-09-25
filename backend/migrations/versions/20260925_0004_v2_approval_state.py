"""增加 V2 审批请求与单会话待审批约束。

Revision ID: 20260925_0004
Revises: 20260925_0003
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260925_0004"
down_revision: str | None = "20260925_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

approval_status = sa.Enum(
    "PENDING",
    "APPROVED",
    "REJECTED",
    "CANCELLED",
    "EXPIRED",
    name="approval_status",
    native_enum=False,
    create_constraint=True,
    length=9,
)
approval_followup_status = sa.Enum(
    "PENDING",
    "SENT",
    "FAILED",
    name="approval_followup_status",
    native_enum=False,
    create_constraint=True,
    length=7,
)


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("offer_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("status", approval_status, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("seller_comment", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("followup_status", approval_followup_status, nullable=True),
        sa.Column("followup_request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "pending_session_id",
            sa.BigInteger(),
            sa.Computed(
                "CASE WHEN status = 'PENDING' THEN session_id ELSE NULL END",
                persisted=True,
            ),
            nullable=True,
        ),
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
            "policy_version > 0",
            name=op.f("ck_approval_requests_policy_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["offers.id"],
            name=op.f("fk_approval_requests_offer_id_offers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["negotiation_sessions.id"],
            name=op.f(
                "fk_approval_requests_session_id_negotiation_sessions"
            ),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_requests")),
        sa.UniqueConstraint(
            "followup_request_id",
            name=op.f("uq_approval_requests_followup_request"),
        ),
        sa.UniqueConstraint(
            "offer_id",
            name=op.f("uq_approval_requests_offer"),
        ),
        sa.UniqueConstraint(
            "pending_session_id",
            name=op.f("uq_approval_requests_pending_session"),
        ),
    )
    op.create_index(
        "ix_approval_requests_session_status",
        "approval_requests",
        ["session_id", "status"],
    )


def downgrade() -> None:
    # 删除整张表会同时删除其索引；MySQL 可能复用该索引支撑外键，不能先拆除。
    op.drop_table("approval_requests")
