"""Run integration tests in disposable MySQL databases, away from the live Worker."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import make_url

from app.core.config import get_settings


def main():
    url = make_url(get_settings().database_url)
    if (
        url.host != "127.0.0.1"
        or url.port != 3307
        or url.database != "secondhand_langgraph"
        or url.username != "langgraph_app"
    ):
        raise RuntimeError("Refusing a database outside the new LangGraph Compose environment")
    suffix = uuid4().hex[:12]
    databases = ["v4_suite_check_" + suffix, "secondhand_agent_migration_test_" + suffix]
    if url.get_backend_name() != "mysql" or any(name == url.database for name in databases):
        raise RuntimeError("Only disposable MySQL databases are allowed")
    username = (url.username or "").replace("'", "''")
    if not username:
        raise RuntimeError("A configured application database user is required")
    command = [
        "docker",
        "exec",
        "-i",
        "secondhand-agent-langgraph-mysql-1",
        "sh",
        "-c",
        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot',
    ]

    def sql(statement):
        result = subprocess.run(command, input=statement, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError("Disposable database operation failed")

    created = []
    backend_root = Path(__file__).resolve().parents[1]
    try:
        for name in databases:
            sql(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4; "
                f"GRANT ALL ON `{name}`.* TO '{username}'@'%';"
            )
            created.append(name)
        environment = {
            **os.environ,
            "DATABASE_URL": url.set(database=databases[0]).render_as_string(hide_password=False),
            "MIGRATION_TEST_DATABASE_URL": url.set(database=databases[1]).render_as_string(
                hide_password=False
            ),
            "RUN_MYSQL_INTEGRATION": "1",
            "RUN_MYSQL_MIGRATION_TESTS": "1",
            "AGENT_RUNTIME": "V3",
            "GRAPH_CHAT_ENABLED": "false",
        }
        migration = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=backend_root,
            env=environment,
            capture_output=True,
        )
        if migration.returncode:
            raise RuntimeError("Disposable database migration failed")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "--tb=short", *sys.argv[1:]],
            cwd=backend_root,
            env=environment,
        )
        return result.returncode
    finally:
        for name in created:
            sql(
                f"DROP DATABASE `{name}`; "
                f"REVOKE ALL PRIVILEGES ON `{name}`.* FROM '{username}'@'%';"
            )
        if created:
            print("Disposable test databases removed; live database and Worker unchanged.")


if __name__ == "__main__":
    raise SystemExit(main())
