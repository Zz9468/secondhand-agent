from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_session_factory_dependency
from app.core.config import Settings, get_settings
from app.core.security import SELLER_SESSION_COOKIE, create_identity_token, hash_password
from app.db.models import NegotiationSession, Product, SellerAccount
from app.main import create_app
from app.services.approval_service import ApprovalService
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import OfferTerms, ShippingPayer
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
    assert TEST_SETTINGS.auth_secret is not None
    token = create_identity_token(
        subject=seller_id,
        kind="seller",
        secret=TEST_SETTINGS.auth_secret.get_secret_value(),
        lifetime=timedelta(minutes=30),
    )
    client.cookies.set(SELLER_SESSION_COOKIE, token)


def _create_pending_approval(
    session_factory: sessionmaker[Session],
) -> tuple[str, int, int]:
    session_id, buyer_id = create_negotiation(session_factory)
    offer = NegotiationService(session_factory).record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=OfferTerms(
            buyer_payment=Decimal("2800.00"),
            shipping_paid_by=ShippingPayer.BUYER,
        ),
    )
    approval = ApprovalService(session_factory).create_request(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        expected_policy_version=1,
        reason="API 审批测试",
        expires_at=datetime.now() + timedelta(hours=1),
    )
    with session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        product = db.get(Product, negotiation.product_id)
        assert product is not None
        return product.seller_id, approval.id, session_id


def test_seller_approval_api_enforces_authentication_and_ownership(
    service_session_factory: sessionmaker[Session],
) -> None:
    seller_id, approval_id, _ = _create_pending_approval(service_session_factory)
    owner = _test_client(service_session_factory)
    outsider = _test_client(service_session_factory)
    anonymous = _test_client(service_session_factory)
    outsider_id = f"seller-{uuid4().hex}"
    with service_session_factory() as db, db.begin():
        db.add(
            SellerAccount(
                id=outsider_id,
                username=f"outsider-{uuid4().hex}",
                password_hash=hash_password("approval-api-test-password"),
                is_active=True,
            )
        )
    _authenticate_seller(owner, seller_id)
    _authenticate_seller(outsider, outsider_id)

    unauthenticated = anonymous.get("/api/seller/approvals")
    listed = owner.get("/api/seller/approvals", params={"status": "PENDING"})
    detail = owner.get(f"/api/seller/approvals/{approval_id}")
    hidden = outsider.get(f"/api/seller/approvals/{approval_id}")

    assert unauthenticated.status_code == 401
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["approvals"]] == [approval_id]
    assert detail.status_code == 200
    assert detail.json()["offer"]["price"] == "2800.00"
    assert detail.json()["status"] == "PENDING"
    assert hidden.status_code == 404


def test_seller_approval_api_approve_and_reject_are_idempotent(
    service_session_factory: sessionmaker[Session],
) -> None:
    seller_id, approval_id, _ = _create_pending_approval(service_session_factory)
    owner = _test_client(service_session_factory)
    _authenticate_seller(owner, seller_id)
    payload = {"request_id": "api-approve-001", "comment": "同意报价"}

    approved = owner.post(
        f"/api/seller/approvals/{approval_id}/approve",
        json=payload,
    )
    repeated = owner.post(
        f"/api/seller/approvals/{approval_id}/approve",
        json=payload,
    )
    conflicting = owner.post(
        f"/api/seller/approvals/{approval_id}/approve",
        json={"request_id": "api-approve-002"},
    )

    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["followup_status"] == "PENDING"
    assert repeated.status_code == 200
    assert repeated.json() == approved.json()
    assert conflicting.status_code == 409

    reject_seller_id, reject_id, _ = _create_pending_approval(
        service_session_factory
    )
    reject_client = _test_client(service_session_factory)
    _authenticate_seller(reject_client, reject_seller_id)
    rejected = reject_client.post(
        f"/api/seller/approvals/{reject_id}/reject",
        json={"request_id": "api-reject-001", "comment": "暂不接受"},
    )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["session_status"] == "ACTIVE"
