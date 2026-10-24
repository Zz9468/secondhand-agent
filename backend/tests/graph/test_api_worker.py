import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_current_user, get_session_factory_dependency
from app.db.models import ModelExecutionTask, NegotiationSession
from app.main import create_app
from app.runtime.contracts import TypedInput
from app.services.chat_service import ChatService
from app.services.errors import NegotiationLifecycleConflictError
from app.services.intent_service import IntentService
from app.services.model_task_service import ModelTaskService
from app.workers.graph_worker import run_batch
from tests.graph.test_business import submit
from tests.graph.test_display_queue import shown


def test_run_status_api_is_owned_and_separates_committed_from_delivery(case):
    factory, sid, buyer, _, queue, runner = case
    run = queue.enqueue(sid, buyer, "one", submit())

    def fail(point):
        if point == "after_command":
            raise RuntimeError("crash")

    runner.fault = fail
    with pytest.raises(RuntimeError):
        runner.run(run)
    app = create_app()
    app.dependency_overrides[get_session_factory_dependency] = lambda: factory
    with TestClient(app) as client:
        path = f"/api/negotiations/{sid}/runs/{run}"
        assert client.get(path).status_code == 401
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=buyer)
        result = client.get(path)
        assert result.status_code == 200
        status = result.json()
        assert status["business_status"] == "COMMITTED"
        assert status["notification_status"] == "PENDING"
        assert status["public_error_category"] == "RESULT_DELIVERY_DELAYED"
        for private in (
            "payload",
            "minimum_net_price",
            "lease_token",
            "config",
            "display_receipt_id",
        ):
            assert private not in status
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="other")
        assert client.get(path).status_code == 404
    schema = app.openapi()["paths"]
    assert "/api/negotiations/{session_id}/inputs" not in schema
    assert "post" not in schema["/api/negotiations/{session_id}/runs/{run_id}"]


def test_legacy_entrypoints_cannot_bypass_graph_protocol(case):
    factory, sid, buyer, _, queue, _ = case
    _, card, _ = shown(case)
    with pytest.raises(NegotiationLifecycleConflictError):
        IntentService(factory).confirm_offer(
            session_id=sid,
            buyer_id=buyer,
            offer_id=card["offer_id"],
            request_id="old-confirm-request",
        )
    with pytest.raises(NegotiationLifecycleConflictError):
        IntentService(factory).close_negotiation(
            session_id=sid, buyer_id=buyer, request_id="old-close-request"
        )
    with pytest.raises(NegotiationLifecycleConflictError):
        ChatService(factory, decision_provider=SimpleNamespace()).send_buyer_message(
            session_id=sid, buyer_id=buyer, request_id="old-chat-request", content="hello"
        )
    pending = queue.enqueue(sid, buyer, "two", TypedInput(kind="QUERY"))
    service = ModelTaskService(factory)
    assert service.lease_task(task_id=queue.read(pending)["task_id"], worker_id="old") is None
    assert service.lease_next(worker_id="old") is None


def test_worker_runs_only_head_and_new_batch_continues_next(case):
    factory, sid, buyer, _, queue, _ = case
    first = queue.enqueue(sid, buyer, "one", submit())
    second = queue.enqueue(sid, buyer, "two", TypedInput(kind="QUERY"))
    run_batch(factory, os.environ["P0_POSTGRES_URL"], limit=100)
    with factory() as db:
        assert db.get(ModelExecutionTask, queue.read(first)["task_id"]).status.value == "SUCCEEDED"
        assert db.get(ModelExecutionTask, queue.read(second)["task_id"]).status.value == "PENDING"
    run_batch(factory, os.environ["P0_POSTGRES_URL"], limit=100)
    with factory() as db:
        assert db.get(NegotiationSession, sid).last_resolved_input_seq == 2


def test_attempt_limit_does_not_clear_fifo_barrier(case):
    _, sid, buyer, clock, queue, runner = case
    first = queue.enqueue(sid, buyer, "one", submit())
    second = queue.enqueue(sid, buyer, "two", TypedInput(kind="CLOSE"))
    for _ in range(3):
        owner = queue.claim(first)
        assert owner
        queue.release(first, owner)
        clock.advance()
    assert queue.claim(first) is None
    assert queue.read(first)["recovery_required"]
    assert runner.run(second) is None
