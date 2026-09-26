from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_session_factory_dependency
from app.core.config import Settings, get_settings
from app.core.security import BUYER_SESSION_COOKIE, create_identity_token
from app.db.models import Message, MessageRole
from app.main import create_app
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import OfferTerms, ShippingPayer
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


def _client(
    session_factory: sessionmaker[Session],
    buyer_id: str,
) -> TestClient:
    application = create_app()
    application.dependency_overrides[get_session_factory_dependency] = (
        lambda: session_factory
    )
    application.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    client = TestClient(application)
    _authenticate_buyer(client, buyer_id)
    return client


def test_confirm_api_records_intent_replays_and_enforces_buyer_identity(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    service = NegotiationService(service_session_factory)
    buyer_offer = service.record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=OfferTerms(
            buyer_payment=Decimal("2750.00"),
            shipping_paid_by=ShippingPayer.BUYER,
        ),
    )
    counter = service.submit_counter_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=OfferTerms(
            buyer_payment=Decimal("2850.00"),
            shipping_paid_by=ShippingPayer.BUYER,
        ),
        responding_to_offer_id=buyer_offer.id,
    )
    with service_session_factory() as db, db.begin():
        db.add(
            Message(
                session_id=session_id,
                role=MessageRole.AGENT,
                content="正式还价测试消息",
                request_id="intent-api-counter-message",
                agent_outcome="COUNTER_OFFERED",
                formal_offer_id=counter.id,
            )
        )

    client = _client(service_session_factory, buyer_id)
    payload = {
        "offer_id": counter.id,
        "request_id": "intent-api-confirm-001",
    }
    confirmed = client.post(
        f"/api/negotiations/{session_id}/confirm",
        json=payload,
    )
    replay = client.post(
        f"/api/negotiations/{session_id}/confirm",
        json=payload,
    )
    state = client.get(f"/api/negotiations/{session_id}")
    history = client.get(f"/api/negotiations/{session_id}/messages")
    attacker = _client(service_session_factory, "another-buyer")
    unauthorized = attacker.post(
        f"/api/negotiations/{session_id}/confirm",
        json=payload,
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "AGREED"
    assert confirmed.json()["confirmation_source"] == "AGENT_COUNTER"
    assert confirmed.json()["idempotent_replay"] is False
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert (
        replay.json()["system_message"]["id"]
        == confirmed.json()["system_message"]["id"]
    )
    assert state.json()["negotiation"]["confirmed_offer_id"] == counter.id
    assert state.json()["negotiation"]["status"] == "AGREED"
    assert history.json()["messages"][-1]["role"] == "SYSTEM"
    assert unauthorized.status_code == 404


def test_close_api_is_idempotent_and_validates_request(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    client = _client(service_session_factory, buyer_id)

    invalid = client.post(
        f"/api/negotiations/{session_id}/close",
        json={"request_id": "bad"},
    )
    closed = client.post(
        f"/api/negotiations/{session_id}/close",
        json={"request_id": "intent-api-close-001"},
    )
    replay = client.post(
        f"/api/negotiations/{session_id}/close",
        json={"request_id": "intent-api-close-retry"},
    )

    assert invalid.status_code == 422
    assert closed.status_code == 200
    assert closed.json()["status"] == "CLOSED"
    assert closed.json()["idempotent_replay"] is False
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
