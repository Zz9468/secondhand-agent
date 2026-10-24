"""Run P0 against disposable real MySQL/PostgreSQL databases, never the old project."""

import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
from dotenv import dotenv_values
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]


def mysql_admin(statement):
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "mysql",
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot',
        ],
        input=statement,
        text=True,
        capture_output=True,
        cwd=ROOT,
    )
    if result.returncode:
        raise RuntimeError("isolated MySQL administration failed")


def pg_admin_config(config, database="postgres"):
    return make_conninfo(
        host="127.0.0.1",
        port=config["POSTGRES_PORT"],
        user=config["POSTGRES_USER"],
        password=config["POSTGRES_PASSWORD"],
        dbname=database,
        connect_timeout=3,
    )


def initialize_checkpoint(admin_url):
    with psycopg.connect(admin_url, autocommit=True, row_factory=dict_row) as db:
        PostgresSaver(db).setup()
        # PostgreSQL otherwise lets a role terminate other sessions of the same role.
        # This is a dedicated project database; revoke PUBLIC and expose only the guard below.
        db.execute(
            "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_terminate_backend(integer,bigint) FROM PUBLIC"
        )
        db.execute("REVOKE EXECUTE ON FUNCTION pg_catalog.pg_cancel_backend(integer) FROM PUBLIC")
        db.execute("GRANT USAGE ON SCHEMA public TO checkpoint_worker")
        db.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            "TO checkpoint_worker"
        )
        db.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO checkpoint_worker")
        # SECURITY DEFINER is narrowly scoped to a verified worker session in this DB.
        db.execute("""
            CREATE OR REPLACE FUNCTION public.p0_runner_exists(
                target_pid integer, target_start timestamptz, target_app text
            ) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
                SELECT EXISTS(SELECT 1 FROM pg_stat_activity
                    WHERE pid=target_pid AND backend_start=target_start
                    AND application_name=target_app AND usename='checkpoint_worker'
                    AND datname=current_database() AND target_app LIKE 'secondhand-p0:%')
            $$
        """)
        db.execute("""
            CREATE OR REPLACE FUNCTION public.p0_terminate_runner(
                target_pid integer, target_start timestamptz, target_app text
            ) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
                SELECT coalesce((SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                    WHERE pid=target_pid AND backend_start=target_start
                    AND application_name=target_app AND usename='checkpoint_worker'
                    AND datname=current_database() AND target_app LIKE 'secondhand-p0:%'), false)
            $$
        """)
        for function in ("p0_runner_exists", "p0_terminate_runner"):
            db.execute(
                sql.SQL(
                    "REVOKE ALL ON FUNCTION public.{}(integer,timestamptz,text) FROM PUBLIC"
                ).format(sql.Identifier(function))
            )
            db.execute(
                sql.SQL(
                    "GRANT EXECUTE ON FUNCTION public.{}(integer,timestamptz,text) "
                    "TO checkpoint_cleaner"
                ).format(sql.Identifier(function))
            )


def main():
    config = dotenv_values(ROOT / ".env")
    url = make_url(config["DATABASE_URL"])
    if (
        url.host != "127.0.0.1"
        or url.port != 3307
        or url.database != "secondhand_langgraph"
        or url.username != "langgraph_app"
    ):
        raise RuntimeError("refusing non-P0 MySQL target")
    if config["POSTGRES_DB"] != "secondhand_checkpoints" or config["POSTGRES_PORT"] != "5433":
        raise RuntimeError("refusing non-P0 PostgreSQL target")
    name = "p0_test_" + uuid4().hex[:12]
    assert re.fullmatch(r"p0_test_[0-9a-f]{12}", name)
    admin_url = pg_admin_config(config)
    cleaner_password = secrets.token_hex(24)
    created_mysql = created_pg = False
    with psycopg.connect(admin_url, autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname='checkpoint_worker'").fetchone():
            admin.execute(
                sql.SQL(
                    "CREATE ROLE checkpoint_worker LOGIN PASSWORD {} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
                ).format(sql.Literal(config["CHECKPOINT_PASSWORD"]))
            )
        if not admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname='checkpoint_cleaner'"
        ).fetchone():
            admin.execute(
                "CREATE ROLE checkpoint_cleaner LOGIN NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE NOREPLICATION"
            )
        admin.execute(
            sql.SQL("ALTER ROLE checkpoint_cleaner PASSWORD {}").format(
                sql.Literal(cleaner_password)
            )
        )
    initialize_checkpoint(pg_admin_config(config, config["POSTGRES_DB"]))
    try:
        mysql_admin(
            f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4; "
            f"GRANT ALL ON `{name}`.* TO 'langgraph_app'@'%';"
        )
        created_mysql = True
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created_pg = True
        pg_test_admin = pg_admin_config(config, name)
        initialize_checkpoint(pg_test_admin)
        worker = make_conninfo(config["CHECKPOINT_DATABASE_URL"], dbname=name)
        env = {
            **os.environ,
            "RUN_P0_INTEGRATION": "1",
            "LANGSMITH_TRACING": "false",
            "P0_MYSQL_URL": url.set(database=name).render_as_string(hide_password=False),
            "P0_POSTGRES_URL": worker,
            "P0_POSTGRES_ADMIN_URL": pg_test_admin,
            "P0_POSTGRES_CLEANER_URL": make_conninfo(
                worker, user="checkpoint_cleaner", password=cleaner_password
            ),
        }
        args = sys.argv[1:] or ["tests/p0", "--junitxml=../artifacts/p0/protocol.xml"]
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--tb=short",
                "-o",
                "junit_family=xunit1",
                *args,
            ],
            env=env,
            cwd=ROOT / "backend",
        ).returncode
    finally:
        if created_mysql:
            mysql_admin(
                f"DROP DATABASE `{name}`; REVOKE ALL ON `{name}`.* FROM 'langgraph_app'@'%';"
            )
        if created_pg:
            with psycopg.connect(admin_url, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        print("Only this run's disposable P0 databases were removed.")


if __name__ == "__main__":
    raise SystemExit(main())
