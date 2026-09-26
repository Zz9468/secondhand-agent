"""增加买家意向确认审计字段。

Revision ID: 20260925_0005
Revises: 20260925_0004
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260925_0005"
down_revision: str | None = "20260925_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

confirmation_source = sa.Enum(
    "AGENT_COUNTER",
    "AUTO_ACCEPTED_BUYER_OFFER",
    "SELLER_APPROVED_BUYER_OFFER",
    name="confirmation_source",
    native_enum=False,
    create_constraint=True,
    length=27,
)


def upgrade() -> None:
    op.add_column(
        "negotiation_sessions",
        sa.Column("confirmed_offer_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "negotiation_sessions",
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "negotiation_sessions",
        sa.Column("confirmation_request_id", sa.String(length=56), nullable=True),
    )
    op.add_column(
        "negotiation_sessions",
        sa.Column("confirmation_source", confirmation_source, nullable=True),
    )
    op.create_unique_constraint(
        "uq_negotiation_sessions_confirmation_request_id",
        "negotiation_sessions",
        ["confirmation_request_id"],
    )
    op.create_foreign_key(
        "fk_negotiation_sessions_confirmed_offer_id_offers",
        "negotiation_sessions",
        "offers",
        ["confirmed_offer_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_negotiation_sessions_confirmed_offer_id_offers",
        "negotiation_sessions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_negotiation_sessions_confirmation_request_id",
        "negotiation_sessions",
        type_="unique",
    )
    op.drop_column("negotiation_sessions", "confirmation_source")
    op.drop_column("negotiation_sessions", "confirmation_request_id")
    op.drop_column("negotiation_sessions", "confirmed_at")
    op.drop_column("negotiation_sessions", "confirmed_offer_id")
