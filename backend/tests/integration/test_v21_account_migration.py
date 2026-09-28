import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

from app.core.config import get_settings

pytestmark = pytest.mark.mysql_integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SAFE_DATABASE_PREFIX = "secondhand_agent_migration_test_"
HISTORICAL_ACCOUNT_PASSWORD_HASH = "!HISTORICAL_VISITOR_NO_LOGIN!"


def test_v2_upgrade_downgrade_and_empty_database_full_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在专用临时库验证 V2 数据升级、回退边界和空库全量迁移。"""

    if os.getenv("RUN_MYSQL_MIGRATION_TESTS") != "1":
        pytest.skip("设置 RUN_MYSQL_MIGRATION_TESTS=1 后运行破坏性迁移测试")
    database_url = os.getenv("MIGRATION_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要 MIGRATION_TEST_DATABASE_URL 指向专用空 MySQL 测试库")

    parsed_url = make_url(database_url)
    database_name = parsed_url.database or ""
    if parsed_url.get_backend_name() != "mysql" or not database_name.startswith(
        SAFE_DATABASE_PREFIX
    ):
        pytest.fail("迁移测试只允许使用名称带安全前缀的专用 MySQL 数据库")

    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    engine = create_engine(database_url)
    try:
        assert inspect(engine).get_table_names() == []

        command.upgrade(config, "20260925_0005")
        _insert_representative_v2_data(engine)
        command.upgrade(config, "head")
        _assert_upgraded_v21_data(engine)

        command.downgrade(config, "20260925_0005")
        _assert_v2_downgrade_boundary(engine)
        command.upgrade(config, "head")
        _assert_upgraded_v21_data(engine)

        command.downgrade(config, "base")
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
        command.upgrade(config, "head")
        _assert_empty_database_head_schema(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()


def _insert_representative_v2_data(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO seller_accounts
                    (id, username, password_hash, is_active, created_at, updated_at)
                VALUES
                    ('seller-owned', 'seller-owned', '!TEST_HASH!', 1,
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('buyer-collision', 'collision-seller', '!TEST_HASH!', 1,
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO products
                    (id, seller_id, title, description, listed_price, status,
                     created_at, updated_at)
                VALUES
                    (5001, 'seller-owned', '迁移测试商品', '保留 V2 业务关联',
                     3000.00, 'AVAILABLE', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO seller_policies
                    (id, product_id, minimum_net_price, auto_accept_threshold,
                     negotiation_style, max_rounds, version, created_at, updated_at)
                VALUES
                    (5101, 5001, 2700.00, 2850.00, 'BALANCED', 6, 1,
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO negotiation_sessions
                    (id, product_id, buyer_id, status, current_offer_id,
                     confirmed_offer_id, confirmed_at, confirmation_request_id,
                     confirmation_source, round_count, version, created_at, updated_at)
                VALUES
                    (6001, 5001, 'buyer-alpha', 'ACTIVE', NULL,
                     NULL, NULL, NULL, NULL, 0, 1,
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    (6002, 5001, 'buyer-beta', 'AGREED', NULL,
                     NULL, NULL, NULL, NULL, 1, 2,
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    (6003, 5001, 'buyer-collision', 'WAITING_APPROVAL', NULL,
                     NULL, NULL, NULL, NULL, 1, 2,
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO offers
                    (id, session_id, proposer, price, shipping_paid_by,
                     shipping_cost, seller_borne_discount, terms, status,
                     expires_at, created_at)
                VALUES
                    (8001, 6002, 'BUYER', 2900.00, 'buyer', NULL,
                     0.00, JSON_OBJECT(), 'ACCEPTED', NULL, CURRENT_TIMESTAMP),
                    (8002, 6003, 'BUYER', 2800.00, 'buyer', NULL,
                     0.00, JSON_OBJECT(), 'PROPOSED', NULL, CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE negotiation_sessions
                SET current_offer_id = 8001,
                    confirmed_offer_id = 8001,
                    confirmed_at = CURRENT_TIMESTAMP,
                    confirmation_request_id = 'confirm-migration-6002',
                    confirmation_source = 'AUTO_ACCEPTED_BUYER_OFFER'
                WHERE id = 6002
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE negotiation_sessions
                SET current_offer_id = 8002
                WHERE id = 6003
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO messages
                    (id, session_id, role, content, request_id,
                     request_fingerprint, agent_outcome, formal_offer_id, created_at)
                VALUES
                    (7001, 6002, 'SYSTEM', '已记录交易意向', 'migration-message',
                     NULL, NULL, 8001, CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO approval_requests
                    (id, session_id, offer_id, policy_version, status, reason,
                     seller_comment, expires_at, reviewed_at, followup_status,
                     followup_request_id, created_at, updated_at)
                VALUES
                    (9001, 6003, 8002, 1, 'PENDING', '迁移测试审批',
                     NULL, DATE_ADD(CURRENT_TIMESTAMP, INTERVAL 1 DAY), NULL,
                     NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )


def _assert_upgraded_v21_data(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "user_accounts" in tables
    assert "seller_accounts" not in tables
    assert "model_execution_tasks" in tables
    assert "observability_events" in tables
    task_columns = {
        item["name"] for item in inspector.get_columns("model_execution_tasks")
    }
    assert {
        "correlation_id",
        "cached_input_tokens",
        "input_price_per_million",
        "output_price_per_million",
        "cached_input_price_per_million",
        "cost_currency",
    } <= task_columns

    product_foreign_keys = inspector.get_foreign_keys("products")
    buyer_foreign_keys = inspector.get_foreign_keys("negotiation_sessions")
    assert any(
        item["referred_table"] == "user_accounts"
        and item["constrained_columns"] == ["seller_id"]
        for item in product_foreign_keys
    )
    assert any(
        item["referred_table"] == "user_accounts"
        and item["constrained_columns"] == ["buyer_id"]
        for item in buyer_foreign_keys
    )

    with engine.connect() as connection:
        session_buyers = {
            row.id: row.buyer_id
            for row in connection.execute(
                text("SELECT id, buyer_id FROM negotiation_sessions ORDER BY id")
            )
        }
        assert session_buyers[6001] == "buyer-alpha"
        assert session_buyers[6002] == "buyer-beta"
        assert session_buyers[6003] != "buyer-collision"
        assert len(set(session_buyers.values())) == 3

        collision_seller = connection.execute(
            text(
                """
                SELECT username, display_name, is_active
                FROM user_accounts
                WHERE id = 'buyer-collision'
                """
            )
        ).one()
        assert collision_seller.username == "collision-seller"
        assert collision_seller.display_name != collision_seller.username
        assert collision_seller.is_active == 1

        historical_accounts = connection.execute(
            text(
                """
                SELECT id, password_hash, is_active
                FROM user_accounts
                WHERE id IN (:buyer_alpha, :buyer_beta, :buyer_collision)
                """
            ),
            {
                "buyer_alpha": session_buyers[6001],
                "buyer_beta": session_buyers[6002],
                "buyer_collision": session_buyers[6003],
            },
        ).all()
        assert len(historical_accounts) == 3
        assert all(row.is_active == 0 for row in historical_accounts)
        assert all(
            row.password_hash == HISTORICAL_ACCOUNT_PASSWORD_HASH
            for row in historical_accounts
        )

        assert connection.scalar(text("SELECT COUNT(*) FROM products")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM negotiation_sessions")) == 3
        assert connection.scalar(text("SELECT COUNT(*) FROM messages")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM offers")) == 2
        assert connection.scalar(text("SELECT COUNT(*) FROM approval_requests")) == 1
        assert connection.scalar(
            text("SELECT confirmed_offer_id FROM negotiation_sessions WHERE id = 6002")
        ) == 8001


def _assert_v2_downgrade_boundary(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "seller_accounts" in tables
    assert "user_accounts" not in tables
    assert "model_execution_tasks" not in tables
    assert not any(
        item["constrained_columns"] == ["buyer_id"]
        for item in inspector.get_foreign_keys("negotiation_sessions")
    )
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM seller_accounts")) == 2
        assert connection.scalar(text("SELECT COUNT(*) FROM negotiation_sessions")) == 3
        assert connection.scalar(text("SELECT COUNT(*) FROM messages")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM offers")) == 2
        assert connection.scalar(text("SELECT COUNT(*) FROM approval_requests")) == 1
        assert connection.scalar(
            text("SELECT seller_id FROM products WHERE id = 5001")
        ) == "seller-owned"


def _assert_empty_database_head_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {
        "user_accounts",
        "products",
        "seller_policies",
        "negotiation_sessions",
        "messages",
        "model_execution_tasks",
        "observability_events",
        "offers",
        "approval_requests",
    } <= tables
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM user_accounts")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM products")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM negotiation_sessions")) == 0
