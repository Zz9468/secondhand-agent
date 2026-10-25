"""部署 checkpoint schema 和受限清理角色，秘密只从环境读取。"""

import os

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.persistence.setup import initialize_checkpoint


def main():
    admin_url = make_conninfo(
        host=os.getenv("POSTGRES_HOST", "checkpoint"),
        port=5432,
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
        connect_timeout=5,
    )
    with psycopg.connect(admin_url, autocommit=True) as db:
        for role, key in (
            ("checkpoint_worker", "CHECKPOINT_PASSWORD"),
            ("graph_cleaner", "GRAPH_CLEANER_PASSWORD"),
        ):
            if not db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                db.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
                    ).format(sql.Identifier(role))
                )
            db.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(os.environ[key])
                )
            )
    initialize_checkpoint(admin_url)
    print("Checkpoint schema and restricted roles ready.")


if __name__ == "__main__":
    main()
