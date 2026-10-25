"""只恢复到本次随机创建的临时库，核对关键业务和 checkpoint 表后删除临时库。"""

import hashlib
import json
import subprocess
from uuid import uuid4

from scripts.bootstrap_p6 import ROOT


def verify(command, directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["sha256"].items():
        with (directory / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError("backup hash mismatch")
    name = "p6_restore_" + uuid4().hex[:12]

    def execute(service, args, data=None):
        result = subprocess.run(
            [*command, "exec", "-T", service, *args],
            cwd=ROOT,
            input=data,
            capture_output=True,
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError(f"{service} restore verification failed")
        return result.stdout.decode("utf-8").strip()

    def mysql(statement):
        return execute(
            "mysql",
            ["sh", "-c", 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N -B'],
            statement.encode(),
        )

    def postgres(statement):
        return execute(
            "checkpoint",
            ["sh", "-c", 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1'],
            statement.encode(),
        )

    mysql_created = pg_created = False
    try:
        mysql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4;")
        mysql_created = True
        execute(
            "mysql",
            ["sh", "-c", f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot {name}'],
            (directory / "mysql.sql").read_bytes(),
        )
        postgres(f'CREATE DATABASE "{name}";')
        pg_created = True
        execute(
            "checkpoint",
            [
                "sh",
                "-c",
                f'exec pg_restore -U "$POSTGRES_USER" -d {name} '
                "--no-owner --no-acl --exit-on-error",
            ],
            (directory / "checkpoint.dump").read_bytes(),
        )
        mysql_tables = [
            "negotiation_sessions",
            "messages",
            "business_action_receipts",
            "model_call_ledger",
            "graph_execution_instances",
            "alembic_version",
        ]
        counts = {}
        for table in mysql_tables:
            pair = mysql(
                f"SELECT COUNT(*) FROM secondhand_langgraph.{table}; "
                f"SELECT COUNT(*) FROM {name}.{table};"
            ).splitlines()
            assert len(pair) == 2 and pair[0] == pair[1], table
            counts[table] = int(pair[0])
        for table in (
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
            "checkpoint_migrations",
        ):
            source = postgres(f"SELECT COUNT(*) FROM {table};")
            restored = execute(
                "checkpoint",
                ["sh", "-c", f'exec psql -U "$POSTGRES_USER" -d {name} -At -v ON_ERROR_STOP=1'],
                f"SELECT COUNT(*) FROM {table};".encode(),
            )
            assert source == restored, table
            counts[table] = int(source)
        return {"status": "PASS", "restored_table_counts": counts}
    finally:
        if mysql_created:
            mysql(f"DROP DATABASE `{name}`;")
        if pg_created:
            postgres(f'DROP DATABASE "{name}";')
