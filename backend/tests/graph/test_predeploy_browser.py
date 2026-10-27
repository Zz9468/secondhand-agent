"""D0 browser fixture: real isolated dual databases; no production test endpoints or models.

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
from fastapi import HTTPException, Request
from sqlalchemy import func, select

from app.api.dependencies import get_current_user, get_session_factory_dependency
from app.core.config import get_settings
from app.db.models import ApprovalRequest, ModelExecutionTask, NegotiationSession, Product
from app.db.models.graph import (
    BusinessActionReceipt,
    GraphRecoveryAction,
    NegotiationInput,
    OutboxEvent,
)
from app.main import create_app
from app.runtime.graph_runner import GraphRunner
from app.runtime.input_queue import InputQueue
from app.semantic.contracts import QUALITY_VERSION, ChatInput
from app.semantic.model import SemanticModel
from app.semantic.quality import QualityModel
from tests.graph.conftest import Clock
from tests.graph.test_p4_quality import FakeQuality
from tests.graph.test_semantic import FakeSemanticModel, candidate, fact, intent
from tests.integration.factories import create_negotiation


@pytest.mark.skipif(os.getenv("RUN_PREDEPLOY_BROWSER") != "1", reason="opt-in browser gate")
def test_predeploy_browser(graph_factory, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    report = Path(os.environ.get("PREDEPLOY_REPORT_DIR", root / "artifacts/predeploy/browser"))
    report.mkdir(parents=True, exist_ok=True)
    cases, observations = {}, []
    external_attempts = []

    def no_external_model(*args, **kwargs):
        external_attempts.append("blocked")
        raise AssertionError("D0 must never invoke a real model")

    monkeypatch.setattr(SemanticModel, "__call__", no_external_model)
    monkeypatch.setattr(QualityModel, "generate", no_external_model)
    settings = get_settings().model_copy(
        update={
            "checkpoint_database_url": os.environ["P0_POSTGRES_URL"],
            "graph_require_services": False,
            "graph_langsmith_tracing": False,
        }
    )
    app = create_app()
    app.dependency_overrides[get_session_factory_dependency] = lambda: graph_factory
    app.dependency_overrides[get_settings] = lambda: settings

    def actor(request: Request):
        # A fixture identity, not an authentication/real-cookie acceptance test.
        user = request.headers.get("x-predeploy-actor")
        for c in cases.values():
            if user in {c.buyer, c.seller}:
                return SimpleNamespace(id=user, username=user, display_name="D0 fixture user")
        raise HTTPException(401, "fixture actor required")

    app.dependency_overrides[get_current_user] = actor

    @app.post("/api/__predeploy/seed/{mode}")
    def seed(mode: str):
        assert mode in {"empty", "offer", "approval", "failed"}
        sid, buyer = create_negotiation(
            graph_factory,
            minimum_net_price=Decimal("750"),
            auto_accept_threshold=Decimal("850"),
        )
        clock = Clock()
        queue = InputQueue(graph_factory, clock=clock)
        queue.enroll(sid, buyer, version=QUALITY_VERSION)
        model, quality = FakeSemanticModel(), FakeQuality()
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
            model.values[text] = {"intents": [intent(text, candidate=candidate(amount))]}
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
                "inputs": [
                    {
                        "run_id": r.id,
                        "request_id": r.request_id,
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
                timeout=360,
            )
            (report / "browser-console.txt").write_text(
                result.stdout + result.stderr, encoding="utf-8"
            )
            assert result.returncode == 0, result.stdout + result.stderr
        finally:
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
