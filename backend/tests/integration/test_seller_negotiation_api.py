from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_session_factory_dependency
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.models import Message, MessageRole, NegotiationSession, Product, UserAccount
from app.main import create_app
from app.services.approval_service import ApprovalService
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import OfferTerms, ShippingPayer
from tests.integration.auth_helpers import authenticate_user
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration

TEST_SETTINGS = Settings(
    _env_file=None,
    database_url="mysql+pymysql://test:test@127.0.0.1/test",
    auth_secret="a-secure-test-secret-with-32-characters",
)


def _test_client(session_factory: sessionmaker[Session]) -> TestClient:
    application = create_app()
    application.dependency_overrides[get_session_factory_dependency] = (
        lambda: session_factory
    )
    application.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    return TestClient(application)


def _authenticate_seller(client: TestClient, seller_id: str) -> None:
    authenticate_user(client, user_id=seller_id, settings=TEST_SETTINGS)


def _create_waiting_negotiation(
    session_factory: sessionmaker[Session],
) -> tuple[str, int, int, int]:
    session_id, buyer_id = create_negotiation(session_factory)
    offer = NegotiationService(session_factory).record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=OfferTerms(
            buyer_payment=Decimal("2800.00"),
            shipping_paid_by=ShippingPayer.BUYER,
        ),
        additional_terms={"delivery_method": "shipping"},
    )
    approval = ApprovalService(session_factory).create_request(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        expected_policy_version=1,
        reason="阶段八卖家会话详情测试",
        expires_at=datetime.now() + timedelta(hours=1),
    )
    with session_factory() as db, db.begin():
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        product = db.get(Product, negotiation.product_id)
        assert product is not None
        db.add(
            Message(
                session_id=session_id,
                role=MessageRole.BUYER,
                content="2800 元可以吗？",
                request_id=f"seller-view-{uuid4().hex}",
                formal_offer_id=offer.id,
            )
        )
        return product.seller_id, session_id, offer.id, approval.id


def test_seller_negotiation_api_lists_owned_sessions_and_detail(
    service_session_factory: sessionmaker[Session],
) -> None:
    seller_id, session_id, offer_id, approval_id = _create_waiting_negotiation(
        service_session_factory
    )
    owner = _test_client(service_session_factory)
    anonymous = _test_client(service_session_factory)
    _authenticate_seller(owner, seller_id)

    unauthenticated = anonymous.get("/api/seller/negotiations")
    listed = owner.get(
        "/api/seller/negotiations",
        params={"status": "WAITING_APPROVAL"},
    )
    detail = owner.get(f"/api/seller/negotiations/{session_id}")
    empty_filter = owner.get(
        "/api/seller/negotiations",
        params={"status": "AGREED"},
    )

    assert unauthenticated.status_code == 401
    assert listed.status_code == 200
    summaries = listed.json()["negotiations"]
    assert [item["id"] for item in summaries] == [session_id]
    assert summaries[0]["current_offer"]["id"] == offer_id
    assert summaries[0]["latest_approval"]["id"] == approval_id
    assert summaries[0]["latest_approval"]["status"] == "PENDING"
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["negotiation"]["id"] == session_id
    assert payload["messages"][0]["content"] == "2800 元可以吗？"
    assert payload["offers"][0]["id"] == offer_id
    assert payload["approvals"][0]["id"] == approval_id
    assert "buyer_id" not in detail.text
    assert "minimum_net_price" not in detail.text
    assert empty_filter.status_code == 200
    assert empty_filter.json()["negotiations"] == []


def test_seller_negotiation_api_hides_other_seller_sessions(
    service_session_factory: sessionmaker[Session],
) -> None:
    _, session_id, _, _ = _create_waiting_negotiation(service_session_factory)
    outsider_id = f"seller-{uuid4().hex}"
    with service_session_factory() as db, db.begin():
        db.add(
            UserAccount(
                id=outsider_id,
                username=f"outsider-{uuid4().hex}",
                display_name="协商接口外部卖家",
                password_hash=hash_password("seller-negotiation-test-password"),
                is_active=True,
            )
        )
    outsider = _test_client(service_session_factory)
    _authenticate_seller(outsider, outsider_id)

    listed = outsider.get("/api/seller/negotiations")
    hidden = outsider.get(f"/api/seller/negotiations/{session_id}")

    assert listed.status_code == 200
    assert listed.json()["negotiations"] == []
    assert hidden.status_code == 404
