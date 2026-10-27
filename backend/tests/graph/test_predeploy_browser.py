"""Predeploy browser fixture: isolated dual databases and real cookie authentication.

Opt in with RUN_PREDEPLOY_BROWSER=1 through scripts.run_p0_tests. The harness binds
an ephemeral loopback port and is destroyed before the runner removes its databases.
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy import func, select

from app.api.dependencies import get_session_factory_dependency
from app.core.config import get_settings
from app.db.models import ApprovalRequest, ModelExecutionTask, NegotiationSession, Product
from app.db.models.graph import (
    BusinessActionReceipt,
    GraphRecoveryAction,
    NegotiationInput,
    NotificationDelivery,
    OutboxEvent,
)
from app.main import create_app
from app.runtime.approval_runner import ApprovalRunner
from app.runtime.graph_runner import GraphRunner
from app.runtime.input_queue import InputQueue
from app.runtime.recovery import RecoveryConflict
from app.semantic.contracts import QUALITY_VERSION, ChatInput
from app.semantic.model import SemanticModel
from app.semantic.quality import QualityModel
from app.services.graph_approval_service import GraphApprovalService
from app.services.graph_recovery_service import GraphRecoveryService
from app.services.graph_status_service import GraphStatusService
from app.services.notification_service import NotificationService
from tests.graph.conftest import Clock
from tests.graph.test_p4_quality import FakeQuality
from tests.graph.test_semantic import FakeSemanticModel, battery_condition, candidate, fact, intent
from tests.integration.factories import create_negotiation


@pytest.mark.skipif(os.getenv("RUN_PREDEPLOY_BROWSER") != "1", reason="opt-in browser gate")
def test_predeploy_browser(graph_factory, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    report = Path(os.environ.get("PREDEPLOY_REPORT_DIR", root / "artifacts/predeploy/browser"))
    report.mkdir(parents=True, exist_ok=True)
    cases, observations = {}, []
    gates = {}
    external_attempts = []

    def no_external_model(*args, **kwargs):
        external_attempts.append("blocked")
        raise AssertionError("Predeploy browser tests must never invoke a real model")

    monkeypatch.setattr(SemanticModel, "__call__", no_external_model)
    monkeypatch.setattr(QualityModel, "generate", no_external_model)
    settings = get_settings().model_copy(
        update={
            "checkpoint_database_url": os.environ["P0_POSTGRES_URL"],
            "auth_secret": SecretStr("d1-isolated-browser-only-auth-secret"),
            "auth_cookie_secure": False,
            "graph_require_services": False,
            "graph_langsmith_tracing": False,
        }
    )
    app = create_app()
    app.dependency_overrides[get_session_factory_dependency] = lambda: graph_factory
    app.dependency_overrides[get_settings] = lambda: settings

    original_enqueue = InputQueue.enqueue

    def gated_enqueue(queue, sid, buyer, request_id, *args, **kwargs):
        gate = gates.get((sid, request_id))
        if gate and not gate["entered"].is_set():
            gate["entered"].set()
            assert gate["release"].wait(30), "test gate was not released"
            try:
                return original_enqueue(queue, sid, buyer, request_id, *args, **kwargs)
            finally:
                gate["finished"].set()
        return original_enqueue(queue, sid, buyer, request_id, *args, **kwargs)

    monkeypatch.setattr(InputQueue, "enqueue", gated_enqueue)

    def wait_for_gate(key, call):
        gate = gates.get(key)
        if gate and not gate["entered"].is_set():
            gate["entered"].set()
            assert gate["release"].wait(30), "test gate was not released"
            try:
                return call()
            finally:
                gate["finished"].set()
        return call()

    original_review, original_act = GraphApprovalService.review, GraphRecoveryService.act

    def gated_review(service, **kwargs):
        return wait_for_gate(
            (kwargs["approval_id"], kwargs["request_id"]),
            lambda: original_review(service, **kwargs),
        )

    def gated_act(service, seller, target, action, request_id, reason):
        return wait_for_gate(
            (target, request_id),
            lambda: original_act(service, seller, target, action, request_id, reason),
        )

    monkeypatch.setattr(GraphApprovalService, "review", gated_review)
    monkeypatch.setattr(GraphRecoveryService, "act", gated_act)

    @app.post("/api/__predeploy/seller-gate/{kind}/{target}/{request_id}/{action}")
    def seller_gate(kind: str, target: str, request_id: str, action: str):
        key = (int(target) if kind == "approval" else target), request_id
        if action == "create":
            gates[key] = {name: threading.Event() for name in ("entered", "release", "finished")}
        elif action == "release":
            gates[key]["release"].set()
            assert gates[key]["finished"].wait(20)
        return {name: event.is_set() for name, event in gates[key].items()}

    @app.post("/api/__predeploy/gate/{sid}/{request_id}/{action}")
    def gate_control(sid: int, request_id: str, action: str):
        assert sid in cases
        key = sid, request_id
        if action == "create":
            gates[key] = {name: threading.Event() for name in ("entered", "release", "finished")}
        elif action == "release":
            gates[key]["release"].set()
            assert gates[key]["finished"].wait(20)
        return {name: event.is_set() for name, event in gates[key].items()}

    @app.post("/api/__predeploy/seed/{mode}")
    def seed(mode: str, buyer_sid: int | None = None):
        assert mode in {"empty", "offer", "approval", "failed", "draft"}
        sid, buyer = create_negotiation(
            graph_factory,
            minimum_net_price=Decimal("750"),
            auto_accept_threshold=Decimal("850"),
        )
        if buyer_sid is not None:
            buyer = cases[buyer_sid].buyer
            with graph_factory() as db, db.begin():
                db.get(NegotiationSession, sid).buyer_id = buyer
        clock = Clock()
        queue = InputQueue(graph_factory, clock=clock)
        queue.enroll(sid, buyer, version=QUALITY_VERSION)
        model, quality = FakeSemanticModel(), FakeQuality()
        for amount in ("800", "900"):
            text = f"{amount}自提"
            model.values[text] = {"intents": [intent(text, candidate=candidate(amount))]}
        runner = GraphRunner(
            graph_factory,
            os.environ["P0_POSTGRES_URL"],
            queue=queue,
            semantic_provider=model,
            quality_provider=quality,
        )
        with graph_factory() as db:
            product = db.get(Product, db.get(NegotiationSession, sid).product_id)
            pid, seller = product.id, product.seller_id
        c = SimpleNamespace(
            factory=graph_factory,
            sid=sid,
            buyer=buyer,
            seller=seller,
            pid=pid,
            clock=clock,
            queue=queue,
            model=model,
            quality=quality,
            runner=runner,
        )
        cases[sid] = c
        fact(c, "allowed_delivery_methods", ["pickup", "shipping"])
        run = None
        if mode != "empty":
            amount = "800" if mode == "approval" else "900"
            text = f"{amount}自提"
            terms = candidate(amount)
            if mode == "draft":
                text = "如果电池至少90%，900自提"
                terms = candidate(amount, conditions=[battery_condition()])
            model.values[text] = {"intents": [intent(text, candidate=terms)]}
            run = queue.enqueue(
                sid, buyer, uuid4().hex, ChatInput(text=text, client_view_id="fixture-view")
            )
            if mode == "failed":
                with graph_factory() as db, db.begin():
                    row = db.get(NegotiationInput, run)
                    row.recovery_required = True
                    task = db.get(ModelExecutionTask, row.task_id)
                    task.status, task.attempt_count = "FAILED", task.max_attempts
            else:
                runner.run(run)
        return {"sid": sid, "buyer": buyer, "seller": seller, "run": run}

    @app.post("/api/__predeploy/execute/{sid}/{run}")
    def execute(sid: int, run: str):
        c = cases[sid]
        assert c.queue.read(run)["session_id"] == sid
        return c.runner.run(run)

    @app.post("/api/__predeploy/lifecycle/{sid}/{run}/{action}")
    def lifecycle(sid: int, run: str, action: str):
        """Real runner faults/recovery; no manufactured public run statuses."""
        c = cases[sid]
        assert c.queue.read(run)["session_id"] == sid
        if action in {"interrupt", "require-recovery"}:

            def crash(point):
                if action == "require-recovery" and point == "after_claim":
                    raise RecoveryConflict("D2 injected recoverable worker failure")
                if action == "interrupt" and point == "after_command":
                    raise RuntimeError("D2 injected after committed command")

            c.runner.fault = crash
            try:
                with pytest.raises((RuntimeError, RecoveryConflict), match="D2 injected"):
                    c.runner.run(run)
            finally:
                c.runner.fault = lambda point: None
        elif action == "notify-fail":
            original_publish = NotificationService.publish

            def fail_publish(service, run_id, **kwargs):
                if run_id == run:
                    raise RuntimeError("D2 injected notification failure")
                return original_publish(service, run_id, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(NotificationService, "publish", fail_publish)
                for _ in range(3):
                    c.clock.advance(31)
                    with pytest.raises(RuntimeError, match="D2 injected notification"):
                        c.runner.notify(f"turn:{run}:reply")
        elif action == "notify":
            c.clock.advance(31)
            assert c.runner.notify(f"turn:{run}:reply")
        elif action == "resume":
            c.clock.advance(2)
            assert c.runner.run(run)
        elif action == "approve":
            with graph_factory() as db:
                approval = db.scalar(
                    select(ApprovalRequest).where(ApprovalRequest.session_id == sid)
                )
                aid = approval.id
            GraphApprovalService(graph_factory, clock=c.clock).review(
                seller_id=c.seller,
                approval_id=aid,
                request_id=uuid4().hex,
                comment=None,
                approved=True,
                expected_revision=0,
            )
            ApprovalRunner(graph_factory, os.environ["P0_POSTGRES_URL"], clock=c.clock).run(aid)
        else:
            raise AssertionError(f"unknown lifecycle action: {action}")
        status = GraphStatusService(graph_factory).get(sid, c.buyer, run)
        observations.append({"lifecycle": action, "sid": sid, "status": status})
        return status

    @app.get("/api/__predeploy/inspect/{sid}")
    def inspect(sid: int):
        assert sid in cases
        with graph_factory() as db:
            inputs = list(
                db.scalars(
                    select(NegotiationInput)
                    .where(NegotiationInput.session_id == sid)
                    .order_by(NegotiationInput.input_seq)
                )
            )
            approvals = list(
                db.scalars(select(ApprovalRequest).where(ApprovalRequest.session_id == sid))
            )
            snapshot = {
                "sid": sid,
                "session_status": db.get(NegotiationSession, sid).status.value,
                "deliveries": [
                    {"source": d.id, "status": d.status, "message_id": d.public_message_id}
                    for d in db.scalars(
                        select(NotificationDelivery).where(
                            NotificationDelivery.input_id.in_([r.id for r in inputs])
                        )
                    )
                ],
                "inputs": [
                    {
                        "run_id": r.id,
                        "request_id": r.request_id,
                        "payload_hash": r.input_hash,
                        "task_status": db.get(ModelExecutionTask, r.task_id).status.value,
                        "manual_retry_count": db.get(
                            ModelExecutionTask, r.task_id
                        ).manual_retry_count,
                        "config_hash": r.config_hash,
                        "active_ms": r.active_ms,
                    }
                    for r in inputs
                ],
                "business_receipts": db.scalar(
                    select(func.count())
                    .select_from(BusinessActionReceipt)
                    .where(BusinessActionReceipt.input_id.in_([r.id for r in inputs]))
                ),
                "recovery_actions": db.scalar(
                    select(func.count())
                    .select_from(GraphRecoveryAction)
                    .where(GraphRecoveryAction.session_id == sid)
                ),
                "approvals": [{"id": a.id, "status": a.status.value} for a in approvals],
                "approval_decisions": db.scalar(
                    select(func.count())
                    .select_from(OutboxEvent)
                    .where(OutboxEvent.id.in_([f"approval:{a.id}:decision" for a in approvals]))
                ),
            }
        observations.append(snapshot)
        return snapshot

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        api_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started, "fixture API did not start"
            env = {
                **os.environ,
                "PREDEPLOY_API_URL": api_url,
                "PREDEPLOY_REPORT_DIR": str(report.resolve()),
            }
            command = [shutil.which("npm"), "run", "test:browser"]
            if os.getenv("PREDEPLOY_BROWSER_GREP"):
                command += ["--", "--grep", os.environ["PREDEPLOY_BROWSER_GREP"]]
            result = subprocess.run(
                command,
                cwd=root / "frontend",
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
            (report / "browser-console.txt").write_text(
                result.stdout + result.stderr, encoding="utf-8"
            )
            assert result.returncode == 0, result.stdout + result.stderr
        finally:
            for gate in gates.values():
                gate["release"].set()
            server.should_exit = True
            thread.join(timeout=10)
            (report / "database-observations.json").write_text(
                json.dumps(
                    {
                        "observations": observations,
                        "external_model_attempts": len(external_attempts),
                        "semantic_fixture_calls": sum(c.model.calls for c in cases.values()),
                        "quality_fixture_calls": sum(len(c.quality.calls) for c in cases.values()),
                        "api_stopped": not thread.is_alive(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            assert not thread.is_alive(), "fixture API failed to stop"
            assert not external_attempts
