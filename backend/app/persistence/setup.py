"""仅部署初始化账号执行；请求和普通 Worker 不运行 saver.setup。"""

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql
from psycopg.rows import dict_row


def initialize_checkpoint(admin_url, *, cleaner_role="graph_cleaner"):
    with psycopg.connect(admin_url, autocommit=True, row_factory=dict_row) as db:
        PostgresSaver(db).setup()
        db.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
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
        for name, expression in (
            ("graph_runner_exists", "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE {guard})"),
            (
                "graph_terminate_runner",
                "SELECT coalesce((SELECT pg_terminate_backend(pid,5000) "
                "FROM pg_stat_activity WHERE {guard}), false)",
            ),
        ):
            guard = (
                "pid=target_pid AND backend_start=target_start AND application_name=target_app "
                "AND usename='checkpoint_worker' AND datname=current_database() "
                "AND target_app ~ '^secondhand-p0:graph-[0-9a-f]{32}$'"
            )
            body = expression.format(guard=guard)
            db.execute(
                sql.SQL(
                    "CREATE OR REPLACE FUNCTION public.{}(target_pid integer, "
                    "target_start timestamptz, target_app text) RETURNS boolean "
                    "LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS {}"
                ).format(sql.Identifier(name), sql.Literal(body))
            )
            db.execute(
                sql.SQL(
                    "REVOKE ALL ON FUNCTION public.{}(integer,timestamptz,text) FROM PUBLIC"
                ).format(sql.Identifier(name))
            )
            db.execute(
                sql.SQL(
                    "GRANT EXECUTE ON FUNCTION public.{}(integer,timestamptz,text) TO {}"
                ).format(sql.Identifier(name), sql.Identifier(cleaner_role))
            )
