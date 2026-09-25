from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import (
    get_decision_provider,
    get_session_factory_dependency,
)
from app.core.config import Settings, get_settings
from app.core.security import BUYER_SESSION_COOKIE, create_identity_token
from app.db.models import NegotiationSession
from app.main import create_app
from tests.fakes import RoutingDecisionProvider, demo_negotiation_decision
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration

TEST_SETTINGS = Settings(
    _env_file=None,
    database_url="mysql+pymysql://test:test@127.0.0.1/test",
    auth_secret="a-secure-test-secret-with-32-characters",
)


def _authenticate_buyer(client: TestClient, buyer_id: str) -> None:
    assert TEST_SETTINGS.auth_secret is not None
    token = create_identity_token(
        subject=buyer_id,
        kind="buyer",
        secret=TEST_SETTINGS.auth_secret.get_secret_value(),
        lifetime=timedelta(minutes=30),
    )
    client.cookies.set(BUYER_SESSION_COOKIE, token)


def test_signed_visitors_create_isolated_product_sessions(
    service_session_factory: sessionmaker[Session],
) -> None:
    existing_session_id, _ = create_negotiation(service_session_factory)
    with service_session_factory() as db:
        existing = db.get(NegotiationSession, existing_session_id)
        assert existing is not None
        product_id = existing.product_id

    application = create_app()
    application.dependency_overrides[get_session_factory_dependency] = (
        lambda: service_session_factory
    )
    application.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    first_browser = TestClient(application)
    second_browser = TestClient(application)
    assert first_browser.post("/api/auth/visitor").status_code == 200
    assert second_browser.post("/api/auth/visitor").status_code == 200

    first = first_browser.post(
        "/api/negotiations",
        json={"product_id": product_id},
    )
    first_replay = first_browser.post(
        "/api/negotiations",
        json={"product_id": product_id},
    )
    second = second_browser.post(
        "/api/negotiations",
        json={"product_id": product_id},
    )

    assert first.status_code == 200
    assert first.json()["created"] is True
    assert first_replay.json() == {
        "session_id": first.json()["session_id"],
        "created": False,
    }
    assert second.status_code == 200
    assert second.json()["session_id"] != first.json()["session_id"]
    assert (
        second_browser.get(
            f"/api/negotiations/{first.json()['session_id']}"
        ).status_code
        == 404
    )


def test_negotiation_api_completes_chat_round_and_enforces_identity(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    provider = RoutingDecisionProvider(demo_negotiation_decision)
    application = create_app()
    application.dependency_overrides[get_session_factory_dependency] = (
        lambda: service_session_factory
    )
    application.dependency_overrides[get_decision_provider] = lambda: provider
    application.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    client = TestClient(application)
    _authenticate_buyer(client, buyer_id)

    state_response = client.get(f"/api/negotiations/{session_id}")
    send_response = client.post(
        f"/api/negotiations/{session_id}/messages",
        json={
            "request_id": "api-request-001",
            "content": "2900 元，不包邮。",
            "offer": {
                "price": "2900.00",
                "shipping_paid_by": "buyer",
                "delivery_method": "shipping",
            },
        },
    )
    history_response = client.get(
        f"/api/negotiations/{session_id}/messages",
    )
    attacker = TestClient(application)
    forged_header_response = attacker.get(
        f"/api/negotiations/{session_id}",
        headers={"X-Buyer-ID": buyer_id},
    )
    _authenticate_buyer(attacker, "another-buyer")
    unauthorized_response = attacker.get(f"/api/negotiations/{session_id}")

    assert state_response.status_code == 200
    assert state_response.json()["product"]["listed_price"] == "3000.00"
    assert send_response.status_code == 201
    body = send_response.json()
    assert body["outcome"] == "OFFER_ACCEPTED"
    assert body["formal_offer_id"] is not None
    assert "2900.00 元" in body["agent_message"]["content"]
    assert [item["role"] for item in history_response.json()["messages"]] == [
        "BUYER",
        "AGENT",
    ]
    assert forged_header_response.status_code == 401
    assert unauthorized_response.status_code == 404


def test_negotiation_api_validates_offer_and_replays_duplicate_request(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    provider = RoutingDecisionProvider(demo_negotiation_decision)
    application = create_app()
    application.dependency_overrides[get_session_factory_dependency] = (
        lambda: service_session_factory
    )
    application.dependency_overrides[get_decision_provider] = lambda: provider
    application.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    client = TestClient(application)
    _authenticate_buyer(client, buyer_id)
    payload = {
        "request_id": "api-request-idempotent-001",
        "content": "商品还在吗？",
    }

    first = client.post(
        f"/api/negotiations/{session_id}/messages",
        json=payload,
    )
    replay = client.post(
        f"/api/negotiations/{session_id}/messages",
        json=payload,
    )
    conflicting_offer = client.post(
        f"/api/negotiations/{session_id}/messages",
        json={
            **payload,
            "offer": {
                "price": "1.00",
                "shipping_paid_by": "buyer",
            },
        },
    )
    invalid_offer = client.post(
        f"/api/negotiations/{session_id}/messages",
        json={
            "request_id": "api-request-invalid-001",
            "content": "包邮",
            "offer": {
                "price": "2900.00",
                "shipping_paid_by": "seller",
            },
        },
    )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["buyer_message"]["id"] == first.json()["buyer_message"]["id"]
    assert replay.json()["outcome"] == first.json()["outcome"]
    assert replay.json()["formal_offer_id"] == first.json()["formal_offer_id"]
    assert len(provider.requests) == 1
    assert conflicting_offer.status_code == 409
    assert invalid_offer.status_code == 422
