"""D1: owned receipt lookups cannot create work, spend budget or reveal payloads."""

import os

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.dependencies import get_session_factory_dependency
from app.core.config import get_settings
from app.db.models import ModelExecutionTask, NegotiationSession, Product
from app.db.models.graph import (
    BusinessActionReceipt,
    GraphRecoveryAction,
    ModelCallLedger,
    NegotiationInput,
    OutboxEvent,
)
from app.main import create_app
from app.runtime.contracts import TypedInput
from app.services.graph_recovery_service import GraphRecoveryService
from tests.graph.test_approval_lifecycle import pending, review
from tests.graph.test_business import submit
from tests.integration.auth_helpers import authenticate_user
from tests.integration.factories import create_negotiation


def client_for(factory):
    settings = get_settings().model_copy(
        update={
            "auth_secret": "d1-local-test-secret-that-is-not-a-production-secret",
            "auth_cookie_secure": False,
            "checkpoint_database_url": os.environ["P0_POSTGRES_URL"],
        }
    )
    # model_copy does not validate SecretStr.
    from pydantic import SecretStr

    settings.auth_secret = SecretStr(settings.auth_secret)
    app = create_app()
    app.dependency_overrides[get_session_factory_dependency] = lambda: factory
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app), settings


def snapshot(factory):
    with factory() as db:
        return {
            model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in (
                NegotiationInput,
                ModelExecutionTask,
                BusinessActionReceipt,
                GraphRecoveryAction,
                ModelCallLedger,
                OutboxEvent,
            )
        }, [
            (
                r.id,
                r.status,
                r.attempt_count,
                r.manual_retry_count,
                r.max_attempts,
                r.lease_token,
                r.lease_expires_at,
            )
            for r in db.scalars(select(ModelExecutionTask).order_by(ModelExecutionTask.id))
        ]


def test_buyer_request_lookup_ownership_and_no_side_effects(case):
    factory, sid, buyer, _, queue, runner = case
    run = queue.enqueue(sid, buyer, "d1-original", submit())

    def crash(point):
        if point == "after_command":
            raise RuntimeError("lost response before notification")

    runner.fault = crash
    import pytest

    with pytest.raises(RuntimeError):
        runner.run(run)
    other_sid, other_buyer = create_negotiation(factory)
    queue.enroll(other_sid, other_buyer)
    queue.enqueue(other_sid, other_buyer, "d1-other", TypedInput(kind="QUERY"))
    before = snapshot(factory)
    client, settings = client_for(factory)
    path = f"/api/negotiations/{sid}/requests/d1-original"
    with client:
        assert client.get(path).status_code == 401
        authenticate_user(client, user_id=buyer, settings=settings)
        for _ in range(3):
            result = client.get(path)
            assert result.status_code == 200
            status = result.json()
            assert status == client.get(f"/api/negotiations/{sid}/runs/{run}").json()
            assert status["business_status"] == "COMMITTED"
            assert status["notification_status"] == "PENDING"
            assert (
                not {
                    "payload",
                    "config",
                    "lease_token",
                    "display_receipt_id",
                    "minimum_net_price",
                    "client_view_id",
                }
                & status.keys()
            )
        assert client.get(f"/api/negotiations/{sid}/requests/missing").status_code == 404
        assert client.get(f"/api/negotiations/{sid}/requests/d1-other").status_code == 404
        authenticate_user(client, user_id=other_buyer, settings=settings)
        denied = client.get(path)
        assert denied.status_code == 404
        assert denied.json() == client.get(f"/api/negotiations/{sid}/requests/missing").json()
    assert snapshot(factory) == before


def test_recovery_request_lookup_and_replay_preserve_budget(case):
    factory, sid, buyer, clock, queue, _ = case
    run = queue.enqueue(sid, buyer, "d1-failed", submit())
    with factory() as db, db.begin():
        item = db.get(NegotiationInput, run)
        item.recovery_required = True
        config = item.config_hash
        task = db.get(ModelExecutionTask, item.task_id)
        task.status, task.attempt_count = "FAILED", task.max_attempts
        seller = db.get(Product, db.get(NegotiationSession, sid).product_id).seller_id
    service = GraphRecoveryService(factory, os.environ["P0_POSTGRES_URL"], clock=clock)
    service.act(seller, run, "RETRY", "d1-recovery", "test retry")
    before = snapshot(factory)
    client, settings = client_for(factory)
    path = f"/api/seller/graph-recovery/{run}/requests/d1-recovery"
    with client:
        assert client.get(path).status_code == 401
        authenticate_user(client, user_id=seller, settings=settings)
        for _ in range(3):
            assert client.get(path).json() == {"status": "RECORDED", "action": "RETRY"}
        assert client.get(f"{path}missing").status_code == 404
        assert (
            client.get("/api/seller/graph-recovery/other/requests/d1-recovery").status_code == 404
        )
        body = {"action": "RETRY", "request_id": "d1-recovery", "reason": "test retry"}
        assert client.post(f"/api/seller/graph-recovery/{run}", json=body).status_code == 200
        body["reason"] = "changed"
        assert client.post(f"/api/seller/graph-recovery/{run}", json=body).status_code == 409
        authenticate_user(client, user_id=buyer, settings=settings)
        assert client.get(path).status_code == 404
    assert snapshot(factory) == before
    with factory() as db:
        assert db.get(NegotiationInput, run).config_hash == config
        assert db.get(ModelExecutionTask, queue.read(run)["task_id"]).manual_retry_count == 1


def test_approval_request_lookup_uses_exact_receipt(case):
    factory, _, buyer, _, _, _ = case
    approval = pending(case)
    review(approval, request_id="d1-approval")
    before = snapshot(factory)
    client, settings = client_for(factory)
    path = f"/api/seller/approvals/{approval.id}/requests/d1-approval"
    with client:
        assert client.get(path).status_code == 401
        authenticate_user(client, user_id=approval.seller, settings=settings)
        for _ in range(3):
            assert client.get(path).json() == {
                "status": "RECORDED",
                "approval_id": approval.id,
                "decision": "APPROVED",
            }
        assert client.get(f"{path}missing").status_code == 404
        assert (
            client.get(
                f"/api/seller/approvals/{approval.id + 10000}/requests/d1-approval"
            ).status_code
            == 404
        )
        authenticate_user(client, user_id=buyer, settings=settings)
        assert client.get(path).status_code == 404
    assert snapshot(factory) == before
    review(approval, request_id="d1-approval")
    assert snapshot(factory) == before


def test_cookie_change_cannot_replay_another_accounts_operation(case):
    factory, sid, buyer, _, _, _ = case
    client, settings = client_for(factory)
    before = snapshot(factory)
    with client:
        authenticate_user(client, user_id=buyer, settings=settings)
        path = f"/api/negotiations/{sid}/close"
        result = client.post(
            path,
            headers={"X-Expected-User": "different-account"},
            json={
                "request_id": "d1-cookie-race",
                "client_view_id": "view",
            },
        )
        assert result.status_code == 401
    assert snapshot(factory) == before
