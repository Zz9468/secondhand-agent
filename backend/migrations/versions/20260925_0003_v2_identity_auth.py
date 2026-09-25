"""增加 V2 卖家账号并建立商品所有权外键。

Revision ID: 20260925_0003
Revises: 20260924_0002
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260925_0003"
down_revision: str | None = "20260924_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "seller_accounts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_accounts")),
        sa.UniqueConstraint("username", name=op.f("uq_seller_accounts_username")),
    )

    # 先为 V1 已存在的 seller_id 建立禁用账号，再加外键，保证原库可升级。
    op.execute(
        sa.text(
            """
            INSERT INTO seller_accounts
                (id, username, password_hash, is_active, created_at, updated_at)
            SELECT DISTINCT seller_id, seller_id, '!MIGRATED_ACCOUNT_DISABLED!',
                0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM products
            """
        )
    )
    op.create_foreign_key(
        "fk_products_seller_id_seller_accounts",
        "products",
        "seller_accounts",
        ["seller_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_products_seller_id_seller_accounts",
        "products",
        type_="foreignkey",
    )
    op.drop_table("seller_accounts")
