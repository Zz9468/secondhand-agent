import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import (
    get_decision_provider,
    get_session_factory_dependency,
)
from app.main import create_app
from tests.fakes import RoutingDecisionProvider, demo_negotiation_decision
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration


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
    client = TestClient(application)
    headers = {"X-Buyer-ID": buyer_id}

    state_response = client.get(f"/api/negotiations/{session_id}", headers=headers)
    send_response = client.post(
        f"/api/negotiations/{session_id}/messages",
        headers=headers,
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
        headers=headers,
    )
    unauthorized_response = client.get(
        f"/api/negotiations/{session_id}",
        headers={"X-Buyer-ID": "another-buyer"},
    )

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
    client = TestClient(application)
    headers = {"X-Buyer-ID": buyer_id}
    payload = {
        "request_id": "api-request-idempotent-001",
        "content": "商品还在吗？",
    }

    first = client.post(
        f"/api/negotiations/{session_id}/messages",
        headers=headers,
        json=payload,
    )
    replay = client.post(
        f"/api/negotiations/{session_id}/messages",
        headers=headers,
        json=payload,
    )
    conflicting_offer = client.post(
        f"/api/negotiations/{session_id}/messages",
        headers=headers,
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
        headers=headers,
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
