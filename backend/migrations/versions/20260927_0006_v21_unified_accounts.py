"""将卖家账号演进为 V2.1 统一账号并迁移历史访客。

Revision ID: 20260927_0006
Revises: 20260925_0005
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0006"
down_revision: str | None = "20260925_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HISTORICAL_ACCOUNT_PASSWORD_HASH = "!HISTORICAL_VISITOR_NO_LOGIN!"


def upgrade() -> None:
    op.rename_table("seller_accounts", "user_accounts")
    op.add_column(
        "user_accounts",
        sa.Column("display_name", sa.String(length=100), nullable=True),
    )
    # 迁移不能公开复用登录名，因此为旧卖家生成稳定但不含用户名的显示名称。
    op.execute(
        sa.text(
            """
            UPDATE user_accounts
            SET display_name = CONCAT(
                '卖家-',
                LEFT(SHA2(CONCAT('v2.1-display:', id), 256), 8)
            )
            WHERE display_name IS NULL
            """
        )
    )
    op.alter_column(
        "user_accounts",
        "display_name",
        existing_type=sa.String(length=100),
        nullable=False,
    )
    op.drop_constraint(
        "uq_seller_accounts_username",
        "user_accounts",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_user_accounts_username",
        "user_accounts",
        ["username"],
    )

    op.drop_constraint(
        "fk_products_seller_id_seller_accounts",
        "products",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_products_seller_id_user_accounts",
        "products",
        "user_accounts",
        ["seller_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # 先建立一对一迁移映射。若旧访客 ID 与卖家账号 ID 相同，使用独立 ID，
    # 避免把两个原本属于不同身份命名空间的主体错误合并。
    op.execute(
        sa.text(
            """
            CREATE TEMPORARY TABLE v21_legacy_buyer_map (
                legacy_buyer_id VARCHAR(64) NOT NULL,
                user_id VARCHAR(64) NOT NULL,
                PRIMARY KEY (legacy_buyer_id),
                UNIQUE KEY uq_v21_legacy_buyer_map_user_id (user_id)
            ) ENGINE=InnoDB
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO v21_legacy_buyer_map (legacy_buyer_id, user_id)
            SELECT legacy_buyers.buyer_id,
                   CASE
                       WHEN existing.id IS NULL THEN legacy_buyers.buyer_id
                       ELSE CONCAT(
                           'history-',
                           LEFT(
                               SHA2(
                                   CONCAT('v2.1-legacy-buyer:', legacy_buyers.buyer_id),
                                   256
                               ),
                               56
                           )
                       )
                   END
            FROM (
                SELECT DISTINCT buyer_id
                FROM negotiation_sessions
            ) AS legacy_buyers
            LEFT JOIN user_accounts AS existing
                ON existing.id = legacy_buyers.buyer_id
            """
        )
    )
    # 不使用 INSERT IGNORE：任何极小概率的 ID 或用户名冲突都应使迁移失败，
    # 而不是静默把不同访客合并到同一账号。
    op.execute(
        sa.text(
            f"""
            INSERT INTO user_accounts (
                id,
                username,
                display_name,
                password_hash,
                is_active,
                created_at,
                updated_at
            )
            SELECT buyer_map.user_id,
                   CONCAT(
                       'history-',
                       LEFT(
                           SHA2(CONCAT('v2.1-history-username:', buyer_map.legacy_buyer_id), 256),
                           56
                       )
                   ),
                   CONCAT(
                       '历史访客-',
                       LEFT(
                           SHA2(CONCAT('v2.1-history-display:', buyer_map.legacy_buyer_id), 256),
                           8
                       )
                   ),
                   '{HISTORICAL_ACCOUNT_PASSWORD_HASH}',
                   0,
                   CURRENT_TIMESTAMP,
                   CURRENT_TIMESTAMP
            FROM v21_legacy_buyer_map AS buyer_map
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE negotiation_sessions AS sessions
            INNER JOIN v21_legacy_buyer_map AS buyer_map
                ON buyer_map.legacy_buyer_id = sessions.buyer_id
            SET sessions.buyer_id = buyer_map.user_id
            """
        )
    )
    op.execute(sa.text("DROP TEMPORARY TABLE v21_legacy_buyer_map"))

    op.create_index(
        op.f("ix_negotiation_sessions_buyer_id"),
        "negotiation_sessions",
        ["buyer_id"],
    )
    op.create_foreign_key(
        "fk_negotiation_sessions_buyer_id_user_accounts",
        "negotiation_sessions",
        "user_accounts",
        ["buyer_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_negotiation_sessions_buyer_id_user_accounts",
        "negotiation_sessions",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_negotiation_sessions_buyer_id"),
        table_name="negotiation_sessions",
    )
    op.drop_constraint(
        "fk_products_seller_id_user_accounts",
        "products",
        type_="foreignkey",
    )

    # V2 的 buyer_id 没有账号外键。仅清理阶段一生成的历史映射账号，
    # 会话、消息、报价、审批及可能发生冲突后的独立 buyer_id 都继续保留。
    op.execute(
        sa.text(
            f"""
            DELETE accounts
            FROM user_accounts AS accounts
            LEFT JOIN products ON products.seller_id = accounts.id
            WHERE accounts.password_hash = '{HISTORICAL_ACCOUNT_PASSWORD_HASH}'
              AND accounts.is_active = 0
              AND products.id IS NULL
            """
        )
    )
    op.drop_constraint(
        "uq_user_accounts_username",
        "user_accounts",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_seller_accounts_username",
        "user_accounts",
        ["username"],
    )
    op.drop_column("user_accounts", "display_name")
    op.rename_table("user_accounts", "seller_accounts")
    op.create_foreign_key(
        "fk_products_seller_id_seller_accounts",
        "products",
        "seller_accounts",
        ["seller_id"],
        ["id"],
        ondelete="RESTRICT",
    )
