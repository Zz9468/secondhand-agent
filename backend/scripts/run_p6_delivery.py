"""全新 Compose 卷上的真实 HTTP 交付复验；显式真实模型许可，保留所有失败证据。"""

import argparse
import hashlib
import json
import secrets
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from sqlalchemy import func, select

from app.db.models import Message, ModelExecutionTask
from app.db.models.graph import ModelCallLedger, NegotiationInput
from app.runtime.database import graph_session_factory
from scripts.backup_p6 import backup
from scripts.bootstrap_p6 import ROOT, prepare
from scripts.export_graph_execution import explain
from scripts.predeploy_delivery import (
    browser_check,
    ledger_snapshot,
    require_free_ports,
    restore_application,
)
from scripts.verify_p6_backup import verify


def wait_for(check, *, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(1)
    raise TimeoutError("delivery verification timed out")


def run(output, *, browser=False):
    require_free_ports((33307, 55433, 18100, 15174))
    output.mkdir(parents=True, exist_ok=False)
    project = "secondhand-p6-" + uuid4().hex[:10]
    env_file = output / "deployment.env"
    env_file.write_text(
        "MYSQL_PORT=33307\nPOSTGRES_PORT=55433\nAPI_PORT=18100\nFRONTEND_PORT=15174\n",
        encoding="utf-8",
    )
    prepare(env_file, ROOT / ".env")
    config = dotenv_values(env_file)
    command = ["docker", "compose", "--env-file", str(env_file), "-p", project]
    report = {"project": project, "status": "RUNNING", "checks": [], "sessions": []}
    report["git_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    report["worktree_dirty"] = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
    )
    app_root = ROOT / "backend" / "app"
    code_hash = hashlib.sha256()
    for path in sorted(app_root.rglob("*")):
        if path.suffix in {".py", ".txt"}:
            code_hash.update(path.relative_to(app_root).as_posix().encode())
            code_hash.update(path.read_bytes().replace(b"\r\n", b"\n"))
    report["app_sha256_lf"] = code_hash.hexdigest()
    factory = None

    def compose(*args):
        result = subprocess.run([*command, *args], cwd=ROOT, capture_output=True, timeout=240)
        with (output / "compose.log").open("ab") as stream:
            stream.write(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("Compose failed; see local compose.log")

    def checked(name):
        report["checks"].append(name)
        print(json.dumps({"check": name, "status": "PASS"}), flush=True)

    def request(client, method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        if not response.is_success:
            raise RuntimeError(f"HTTP {response.status_code} at {path}")
        return response.json()

    try:
        compose("up", "-d", "--no-build", "frontend")
        with httpx.Client(base_url="http://127.0.0.1:18100", timeout=15) as health:
            ready = wait_for(lambda: health.get("/api/ready").status_code == 200)
            assert ready
        checked("fresh_volumes_migration_checkpoint_roles_services_ready")
        compose(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            "from pathlib import Path; import hashlib; p=Path('/app/app'); h=hashlib.sha256(); "
            "[(h.update(f.relative_to(p).as_posix().encode()), "
            "h.update(f.read_bytes().replace(bytes([13,10]),bytes([10])))) "
            "for f in sorted(p.rglob('*')) if f.suffix in {'.py','.txt'}]; "
            f"assert h.hexdigest() == '{report['app_sha256_lf']}'",
        )
        checked("container_application_matches_verified_source")
        for service in ("api", "worker"):
            compose(
                "exec",
                "-T",
                service,
                "python",
                "-c",
                "import os; assert not any(k in os.environ for k in "
                "['POSTGRES_PASSWORD','MYSQL_ROOT_PASSWORD','GRAPH_CLEANER_PASSWORD',"
                "'CHECKPOINT_CLEANER_URL'])",
            )
        checked("runtime_credentials_exclude_admin_and_cleaner")
        factory = graph_session_factory(config["DATABASE_URL"])
        with (
            httpx.Client(base_url="http://127.0.0.1:18100", timeout=15) as seller,
            httpx.Client(base_url="http://127.0.0.1:18100", timeout=15) as buyer,
        ):
            for name, client in (("seller", seller), ("buyer", buyer)):
                request(
                    client,
                    "POST",
                    "/api/auth/register",
                    json={
                        "username": f"p6-{name}",
                        "display_name": f"P6 {name}",
                        "password": secrets.token_hex(20) + "Aa1!",
                    },
                )
            product = request(
                seller,
                "POST",
                "/api/products",
                json={
                    "title": "P6演示相机",
                    "description": "黑色相机，支持自提。",
                    "listed_price": "1000",
                    "status": "AVAILABLE",
                    "policy": {"minimum_net_price": "750", "auto_accept_threshold": "850"},
                },
            )
            request(
                seller,
                "PUT",
                f"/api/seller/products/{product['id']}/facts/allowed_delivery_methods",
                json={"expected_version": 1, "value": ["pickup"]},
            )
            assert request(buyer, "GET", "/api/products")["products"]
            sid = request(buyer, "POST", "/api/negotiations", json={"product_id": product["id"]})[
                "session_id"
            ]
            report["sessions"].append(sid)
            checked("accounts_product_and_new_graph_session")

            def messages():
                return request(buyer, "GET", f"/api/negotiations/{sid}/messages")["messages"]

            def completed(run_id):
                with factory() as db:
                    item = db.get(NegotiationInput, run_id)
                    task = db.get(ModelExecutionTask, item.task_id)
                    if task.status.value in {"FAILED", "CANCELLED", "STALE"}:
                        raise RuntimeError(f"graph task {task.status.value}")
                    return task.status.value == "SUCCEEDED"

            def chat(text):
                body = {"request_id": uuid4().hex, "client_view_id": "p6-tab", "content": text}
                accepted = request(buyer, "POST", f"/api/negotiations/{sid}/messages", json=body)
                wait_for(lambda: completed(accepted["run_id"]))
                replay = request(buyer, "POST", f"/api/negotiations/{sid}/messages", json=body)
                assert replay["run_id"] == accepted["run_id"] and replay["idempotent_replay"]
                return messages()[-1]

            answer = chat("这台相机是什么颜色？")
            assert "黑" in answer["content"]
            checked("real_model_consultation_and_request_replay")
            chat("800元自提")
            approvals = request(seller, "GET", "/api/seller/approvals")["approvals"]
            approval = next(a for a in approvals if a["session_id"] == sid)
            assert approval["status"] == "PENDING"
            compose("restart", "worker", "watchdog")
            request(
                seller,
                "POST",
                f"/api/seller/approvals/{approval['id']}/approve",
                json={
                    "request_id": uuid4().hex,
                    "comment": "P6交付复验",
                    "expected_effect_revision": approval["effect_revision"],
                },
            )

            def accepted_card():
                rows = messages()
                for row in reversed(rows):
                    card = row.get("card") or {}
                    if card.get("can_confirm") and card.get("context_id"):
                        return card
                return None

            card = wait_for(accepted_card)
            checked("approval_after_worker_restart_and_notification")
            ack = request(
                buyer,
                "POST",
                f"/api/negotiations/{sid}/display-acks",
                json={
                    "request_id": uuid4().hex,
                    "context_id": card["context_id"],
                    "client_view_id": "p6-tab",
                },
            )
            body = {
                "request_id": uuid4().hex,
                "offer_id": card["offer_id"],
                "client_view_id": "p6-tab",
                **ack,
            }
            accepted = request(buyer, "POST", f"/api/negotiations/{sid}/confirm", json=body)
            wait_for(lambda: completed(accepted["run_id"]))
            assert (
                request(buyer, "GET", f"/api/negotiations/{sid}")["negotiation"]["status"]
                == "AGREED"
            )
            before = len(messages())
            assert request(buyer, "POST", f"/api/negotiations/{sid}/confirm", json=body)[
                "idempotent_replay"
            ]
            assert len(messages()) == before
            checked("display_confirm_agreed_and_idempotency")
            history = explain(factory, config["CHECKPOINT_DATABASE_URL"], sid)
            assert {"parse_intents", "commit_business", "publish_reply"} <= {
                name for step in history["steps"] for name in step["next_nodes"]
            }
            (output / "node-history.json").write_text(
                json.dumps(history, indent=2), encoding="utf-8"
            )
            checked("official_checkpoint_node_history")
        with factory() as db:
            calls = list(db.scalars(select(ModelCallLedger)))
            report["model_calls"] = len(calls)
            report["unknown_cost_calls"] = sum(c.actual_cost is None for c in calls)
            report["known_estimated_cost"] = str(sum(c.actual_cost or 0 for c in calls))
            report["message_count"] = db.scalar(select(func.count()).select_from(Message))
        if browser:
            report["browser"] = browser_check(output, project, env_file)
            checked("production_browser_faults_and_buyer_seller_lifecycle")
        compose("stop", "api", "worker", "watchdog")
        report["backup"] = backup(command, output / "backup")
        checked("quiescent_dual_database_backup")
        report["restore_drill"] = verify(command, output / "backup")
        checked("restore_to_disposable_databases")
        if browser:
            report["restored_application"] = restore_application(
                output, wait_for, report["restore_drill"]["restored_table_counts"]
            )
            checked("restored_complete_application_and_permissions")
        report["status"] = "PASS"
    except Exception as exc:
        report["status"], report["error_category"] = "FAIL", type(exc).__name__
        report["error"] = str(exc)
        raise
    finally:
        try:
            compose("logs", "--no-color", "--tail", "80", "api", "worker", "watchdog")
        except Exception as exc:
            report["log_collection_error"] = type(exc).__name__
        if factory:
            try:
                with factory() as db:
                    calls = list(db.scalars(select(ModelCallLedger)))
                    report["model_calls"] = len(calls)
                    report["unknown_cost_calls"] = sum(c.actual_cost is None for c in calls)
                    report["known_estimated_cost"] = str(sum(c.actual_cost or 0 for c in calls))
                report["ledger"] = ledger_snapshot(factory)
            except Exception:
                report["ledger_read_failed"] = True
            finally:
                factory.kw["bind"].dispose()
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # Keep this run's volumes as evidence; no user/runtime volume is deleted.
        try:
            compose("down")
        except Exception as exc:
            report.update(status="FAIL", cleanup_error=type(exc).__name__)
            raise
        finally:
            (output / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-model", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Also run isolated production browser and restored-application checks",
    )
    args = parser.parse_args()
    run(args.output.resolve(), browser=args.browser)


if __name__ == "__main__":
    main()
