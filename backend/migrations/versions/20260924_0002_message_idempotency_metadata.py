"""为聊天消息增加完整幂等与响应重放元数据。

Revision ID: 20260924_0002
Revises: 20260924_0001
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0002"
down_revision: str | None = "20260924_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("agent_outcome", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("formal_offer_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_messages_formal_offer_id_offers",
        "messages",
        "offers",
        ["formal_offer_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_messages_formal_offer_id_offers",
        "messages",
        type_="foreignkey",
    )
    op.drop_column("messages", "formal_offer_id")
    op.drop_column("messages", "agent_outcome")
    op.drop_column("messages", "request_fingerprint")
