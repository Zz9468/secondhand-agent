"""D5 opt-in production browser and restored-application checks; isolated volumes only."""

import hashlib
import json
import socket
import subprocess
from uuid import uuid4

import httpx
import psycopg
from dotenv import dotenv_values
from sqlalchemy import select, text

from app.db.models import ModelExecutionTask, NegotiationSession
from app.db.models.graph import BusinessActionReceipt, ModelCallLedger, NegotiationInput
from app.runtime.database import graph_session_factory
from scripts.bootstrap_p6 import ROOT, prepare


def require_free_ports(ports):
    for port in ports:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))


def ledger_snapshot(factory):
    with factory() as db:
        calls = list(db.scalars(select(ModelCallLedger)))
        return {
            "inputs": [
                {
                    "id": i.id,
                    "session_id": i.session_id,
                    "config": i.config,
                    "config_hash": i.config_hash,
                    "active_ms": i.active_ms,
                }
                for i in db.scalars(select(NegotiationInput))
            ],
            "model_calls": len(calls),
            "known_tokens": sum(c.actual_tokens or 0 for c in calls),
            "unknown_usage_calls": sum(c.actual_tokens is None for c in calls),
            "unknown_cost_calls": sum(c.actual_cost is None for c in calls),
            "known_estimated_cost": str(sum(c.actual_cost or 0 for c in calls)),
            "calls": [
                {
                    "id": c.id,
                    "input_id": c.input_id,
                    "model": c.model_name,
                    "purpose": c.purpose,
                    "status": c.status,
                    "tokens": c.actual_tokens,
                    "cost": str(c.actual_cost) if c.actual_cost is not None else None,
                    "error_category": c.error_category,
                    "seconds": (c.finished_at - c.created_at).total_seconds()
                    if c.finished_at
                    else None,
                }
                for c in calls
            ],
        }


def browser_check(output, project, env_file, *, phase="source", credentials=None, port=15174):
    config = {
        "root": str(ROOT),
        "project": project,
        "envFile": str(env_file),
        "phase": phase,
        "url": f"http://127.0.0.1:{port}",
        "output": str(output / "browser"),
        "credentials": str(credentials or output / "browser-credentials.json"),
    }
    config_path = output / "browser-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with (output / "browser.log").open("xb") as stream:
        result = subprocess.run(
            ["node", str(ROOT / "frontend/tests/delivery/predeploy.mjs"), str(config_path)],
            cwd=ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=600,
        )
    if result.returncode:
        raise RuntimeError(
            "Production browser check failed; see browser/report.json and browser.log"
        )
    observed = json.loads((output / "browser/report.json").read_text(encoding="utf-8"))
    factory = graph_session_factory(dotenv_values(env_file)["DATABASE_URL"])
    try:
        with factory() as db:
            sid = observed["sid"]
            inputs = list(
                db.scalars(select(NegotiationInput).where(NegotiationInput.session_id == sid))
            )
            rows = [
                {
                    "run_id": i.id,
                    "request_id": i.request_id,
                    "input_seq": i.input_seq,
                    "config_hash": i.config_hash,
                    "active_ms": i.active_ms,
                    "task": db.get(ModelExecutionTask, i.task_id).status.value,
                    "receipts": len(
                        list(
                            db.scalars(
                                select(BusinessActionReceipt).where(
                                    BusinessActionReceipt.input_id == i.id
                                )
                            )
                        )
                    ),
                }
                for i in inputs
            ]
            assert len(rows) == 2 and all(r["receipts"] == 1 for r in rows)
            assert all(r["task"] == "SUCCEEDED" for r in rows)
            assert db.get(NegotiationSession, sid).status.value == "AGREED"
            if phase == "source":
                assert {r["request_id"] for r in rows} == {
                    r["request_id"] for r in observed["requests"]
                }
            observed["database_inputs"] = rows
        (output / "browser/database-check.json").write_text(
            json.dumps(observed, indent=2), encoding="utf-8"
        )
    finally:
        factory.kw["bind"].dispose()
    return observed


def restore_application(source, wait_for, expected_counts, *, output=None):
    """Restore both dumps into new Compose volumes, before starting any application writer."""
    require_free_ports((33308, 55434, 18101, 15176))
    output = output or source / "restored-application"
    output.mkdir(exist_ok=False)
    project = "secondhand-p6-" + uuid4().hex[:10]
    env_file = output / "deployment.env"
    env_file.write_text(
        "MYSQL_PORT=33308\nPOSTGRES_PORT=55434\nAPI_PORT=18101\nFRONTEND_PORT=15176\n",
        encoding="utf-8",
    )
    prepare(env_file, source / "deployment.env")
    config = dotenv_values(env_file)
    command = ["docker", "compose", "--env-file", str(env_file), "-p", project]
    report = {"project": project, "status": "RUNNING", "checks": []}
    factory = None

    def compose(*args, data=None):
        result = subprocess.run(
            [*command, *args], cwd=ROOT, input=data, capture_output=True, timeout=240
        )
        with (output / "compose.log").open("ab") as log:
            log.write(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("Restored application Compose step failed; see compose.log")
        return result.stdout

    try:
        manifest = json.loads((source / "backup/manifest.json").read_text(encoding="utf-8"))
        for name, digest in manifest["sha256"].items():
            assert hashlib.sha256((source / "backup" / name).read_bytes()).hexdigest() == digest
        compose("up", "-d", "--wait", "--no-build", "mysql", "checkpoint")
        compose(
            "exec",
            "-T",
            "mysql",
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot "$MYSQL_DATABASE"',
            data=(source / "backup/mysql.sql").read_bytes(),
        )
        compose(
            "exec",
            "-T",
            "checkpoint",
            "sh",
            "-c",
            'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
            "--no-owner --no-acl --exit-on-error",
            data=(source / "backup/checkpoint.dump").read_bytes(),
        )
        report["checks"].append("both_hash_verified_dumps_restored_before_writers")
        # Recreate restricted roles before app reads; migration must not alter the expected head.
        compose("run", "--rm", "checkpoint-setup")
        factory = graph_session_factory(config["DATABASE_URL"])
        with factory() as db:
            for table, count in expected_counts.items():
                if table not in {
                    "checkpoints",
                    "checkpoint_blobs",
                    "checkpoint_writes",
                    "checkpoint_migrations",
                }:
                    assert table in {
                        "negotiation_sessions",
                        "messages",
                        "business_action_receipts",
                        "model_call_ledger",
                        "graph_execution_instances",
                        "alembic_version",
                    }
                    assert db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == count
            assert (
                db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "a67db214f9c3"
            )
        with psycopg.connect(config["CHECKPOINT_DATABASE_URL"]) as pg:
            for table in (
                "checkpoints",
                "checkpoint_blobs",
                "checkpoint_writes",
                "checkpoint_migrations",
            ):
                from psycopg import sql

                assert (
                    pg.execute(
                        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                    ).fetchone()[0]
                    == expected_counts[table]
                )
            assert pg.execute("SELECT count(*) FROM checkpoints").fetchone()[0] > 0
            assert pg.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
            ).fetchone() == (False,)
            assert pg.execute(
                "SELECT has_function_privilege(current_user, "
                "'pg_catalog.pg_terminate_backend(integer,bigint)', 'EXECUTE')"
            ).fetchone() == (False,)
            assert pg.execute(
                "SELECT has_function_privilege(current_user, "
                "'public.graph_terminate_runner(integer,timestamptz,text)', 'EXECUTE')"
            ).fetchone() == (False,)
        with psycopg.connect(config["CHECKPOINT_CLEANER_URL"]) as pg:
            assert pg.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
            ).fetchone() == (False,)
            assert pg.execute(
                "SELECT has_function_privilege(current_user, "
                "'public.graph_terminate_runner(integer,timestamptz,text)', 'EXECUTE')"
            ).fetchone() == (True,)
            assert pg.execute(
                "SELECT public.graph_terminate_runner(0,now(),'unrelated')"
            ).fetchone() == (False,)
        report["checks"].append("restored_migration_checkpoint_and_restricted_roles")
        before = ledger_snapshot(factory)
        report["restored_ledger_ids"] = [c["id"] for c in before["calls"]]
        compose("up", "-d", "--no-build", "frontend")
        with httpx.Client(base_url="http://127.0.0.1:18101", timeout=5) as client:
            wait_for(lambda: client.get("/api/ready").status_code == 200)
        report["checks"].append("restored_api_worker_watchdog_ready")
        report["browser"] = browser_check(
            output,
            project,
            env_file,
            phase="restore",
            credentials=source / "browser-credentials.json",
            port=15176,
        )
        report["status"] = "PASS"
    except Exception as exc:
        report.update(status="FAIL", error_category=type(exc).__name__, error=str(exc))
        raise
    finally:
        try:
            if factory:
                report["ledger"] = ledger_snapshot(factory)
        finally:
            if factory:
                factory.kw["bind"].dispose()
            for args in (
                ("logs", "--no-color", "--tail", "80", "api", "worker", "watchdog"),
                ("down",),
            ):
                try:
                    compose(*args)  # retain this run's restored volumes, including failures
                except Exception as exc:
                    report.setdefault("cleanup_errors", []).append(type(exc).__name__)
                    report["status"] = "FAIL"
                finally:
                    (output / "report.json").write_text(
                        json.dumps(report, indent=2), encoding="utf-8"
                    )
    if report.get("cleanup_errors"):
        raise RuntimeError("Restored application cleanup incomplete; see report.json")
    return report
