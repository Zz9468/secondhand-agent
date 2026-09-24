"""创建 V1 核心业务表。

Revision ID: 20260924_0001
Revises:
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

product_status = sa.Enum(
    "DRAFT",
    "AVAILABLE",
    "UNAVAILABLE",
    name="product_status",
    native_enum=False,
    create_constraint=True,
    length=11,
)
negotiation_style = sa.Enum(
    "FIRM",
    "BALANCED",
    "FLEXIBLE",
    name="negotiation_style",
    native_enum=False,
    create_constraint=True,
    length=8,
)
negotiation_status = sa.Enum(
    "ACTIVE",
    "WAITING_APPROVAL",
    "AGREED",
    "CLOSED",
    name="negotiation_status",
    native_enum=False,
    create_constraint=True,
    length=16,
)
message_role = sa.Enum(
    "BUYER",
    "AGENT",
    "SYSTEM",
    name="message_role",
    native_enum=False,
    create_constraint=True,
    length=6,
)
offer_proposer = sa.Enum(
    "BUYER",
    "AGENT",
    name="offer_proposer",
    native_enum=False,
    create_constraint=True,
    length=5,
)
shipping_payer = sa.Enum(
    "buyer",
    "seller",
    name="shipping_payer",
    native_enum=False,
    create_constraint=True,
    length=6,
)
offer_status = sa.Enum(
    "PROPOSED",
    "ACCEPTED",
    "REJECTED",
    "WITHDRAWN",
    name="offer_status",
    native_enum=False,
    create_constraint=True,
    length=9,
)


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("seller_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("listed_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", product_status, nullable=False),
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
            "listed_price >= 0",
            name=op.f("ck_products_listed_price_nonnegative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
    )
    op.create_index(op.f("ix_products_seller_id"), "products", ["seller_id"])

    op.create_table(
        "seller_policies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("minimum_net_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("auto_accept_threshold", sa.Numeric(12, 2), nullable=False),
        sa.Column("negotiation_style", negotiation_style, nullable=False),
        sa.Column("max_rounds", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
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
            "minimum_net_price >= 0",
            name=op.f("ck_seller_policies_minimum_net_price_nonnegative"),
        ),
        sa.CheckConstraint(
            "auto_accept_threshold >= minimum_net_price",
            name=op.f("ck_seller_policies_threshold_not_below_minimum"),
        ),
        sa.CheckConstraint(
            "max_rounds > 0",
            name=op.f("ck_seller_policies_max_rounds_positive"),
        ),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_seller_policies_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_seller_policies_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_policies")),
        sa.UniqueConstraint("product_id", name=op.f("uq_seller_policies_product")),
    )

    op.create_table(
        "negotiation_sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("buyer_id", sa.String(length=64), nullable=False),
        sa.Column("status", negotiation_status, nullable=False),
        sa.Column("current_offer_id", sa.BigInteger(), nullable=True),
        sa.Column("round_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
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
            "round_count >= 0",
            name=op.f("ck_negotiation_sessions_round_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_negotiation_sessions_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_negotiation_sessions_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_negotiation_sessions")),
    )
    op.create_index(
        "ix_negotiation_sessions_product_buyer",
        "negotiation_sessions",
        ["product_id", "buyer_id"],
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("role", message_role, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["negotiation_sessions.id"],
            name=op.f("fk_messages_session_id_negotiation_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "session_id",
            "request_id",
            name=op.f("uq_messages_session_request"),
        ),
    )

    op.create_table(
        "offers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("proposer", offer_proposer, nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("shipping_paid_by", shipping_payer, nullable=False),
        sa.Column("shipping_cost", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "seller_borne_discount",
            sa.Numeric(12, 2),
            nullable=False,
        ),
        sa.Column("terms", sa.JSON(), nullable=False),
        sa.Column("status", offer_status, nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("price >= 0", name=op.f("ck_offers_price_nonnegative")),
        sa.CheckConstraint(
            "shipping_cost IS NULL OR shipping_cost >= 0",
            name=op.f("ck_offers_shipping_cost_nonnegative"),
        ),
        sa.CheckConstraint(
            "seller_borne_discount >= 0",
            name=op.f("ck_offers_seller_borne_discount_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["negotiation_sessions.id"],
            name=op.f("fk_offers_session_id_negotiation_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offers")),
    )
    op.create_foreign_key(
        "fk_negotiation_sessions_current_offer_id_offers",
        "negotiation_sessions",
        "offers",
        ["current_offer_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_negotiation_sessions_current_offer_id_offers",
        "negotiation_sessions",
        type_="foreignkey",
    )
    op.drop_table("offers")
    op.drop_table("messages")
    op.drop_index("ix_negotiation_sessions_product_buyer", table_name="negotiation_sessions")
    op.drop_table("negotiation_sessions")
    op.drop_table("seller_policies")
    op.drop_index(op.f("ix_products_seller_id"), table_name="products")
    op.drop_table("products")
