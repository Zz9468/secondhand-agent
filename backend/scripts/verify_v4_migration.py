"""Verify fresh MySQL migration and V4 schema rollback in a disposable database."""

import json
import os
import subprocess
import sys
from uuid import uuid4

from sqlalchemy.engine import make_url

from app.core.config import get_settings


def main():
    url = make_url(get_settings().database_url)
    temporary = "v4_migration_check_" + uuid4().hex[:12]
    if not temporary.startswith("v4_migration_check_") or temporary == url.database:
        raise RuntimeError("Refusing to use a non-disposable database")
    username = url.username.replace("'", "''")
    command = [
        "docker",
        "exec",
        "-i",
        "secondhand-agent-mysql-1",
        "sh",
        "-c",
        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot',
    ]

    def sql(statement):
        result = subprocess.run(command, input=statement, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError("Disposable database operation failed")

    checks = []
    granted = False
    try:
        sql(
            f"CREATE DATABASE `{temporary}` CHARACTER SET utf8mb4; "
            f"GRANT ALL ON `{temporary}`.* TO '{username}'@'%';"
        )
        granted = True
        environment = {
            **os.environ,
            "DATABASE_URL": url.set(database=temporary).render_as_string(hide_password=False),
        }
        for args in (
            ("upgrade", "head"),
            ("check",),
            ("downgrade", "-1"),
            ("upgrade", "head"),
            ("check",),
        ):
            result = subprocess.run(
                [sys.executable, "-m", "alembic", *args], env=environment, capture_output=True
            )
            checks.append(
                {"command": "alembic " + " ".join(args), "passed": result.returncode == 0}
            )
            if result.returncode:
                diagnostic = result.stderr.decode("utf-8", errors="replace")
                if url.password:
                    diagnostic = diagnostic.replace(url.password, "<redacted>")
                print(diagnostic[-3000:])
                raise RuntimeError("Migration verification failed: " + checks[-1]["command"])
    finally:
        sql(f"DROP DATABASE IF EXISTS `{temporary}`;")
        if granted:
            sql(f"REVOKE ALL PRIVILEGES ON `{temporary}`.* FROM '{username}'@'%';")
    print(json.dumps({"status": "PASS", "checks": checks, "temporary_database_removed": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
