import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_engine


@pytest.fixture(scope="session")
def mysql_engine() -> Engine:
    """仅在显式启用时连接本地 MySQL，避免普通单元测试依赖外部服务。"""

    if os.getenv("RUN_MYSQL_INTEGRATION") != "1":
        pytest.skip("设置 RUN_MYSQL_INTEGRATION=1 后运行 MySQL 集成测试")

    engine = get_engine()
    if engine.dialect.name != "mysql":
        pytest.skip("当前 DATABASE_URL 不是 MySQL")
    return engine


@pytest.fixture
def mysql_connection(mysql_engine: Engine) -> Iterator[Connection]:
    """每个用例结束后回滚，使集成测试不污染演示数据。"""

    with mysql_engine.connect() as connection:
        connection = connection.execution_options(isolation_level="READ COMMITTED")
        transaction = connection.begin()
        yield connection
        transaction.rollback()


@pytest.fixture
def db_session(mysql_connection: Connection) -> Iterator[Session]:
    with Session(bind=mysql_connection) as db:
        yield db


@pytest.fixture
def service_session_factory(
    mysql_connection: Connection,
) -> sessionmaker[Session]:
    """让 Service 自主管理事务，同时由测试外层事务统一回滚。"""

    return sessionmaker(
        bind=mysql_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
