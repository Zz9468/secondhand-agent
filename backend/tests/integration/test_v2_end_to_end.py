from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.approval_followup import (
    ApprovalFollowupDraft,
    ApprovalFollowupRequest,
)
from app.agent.seller_agent import AgentTurnOutcome
from app.api.dependencies import get_session_factory_dependency
from app.core.config import Settings, get_settings
from app.core.security import (
    BUYER_SESSION_COOKIE,
    SELLER_SESSION_COOKIE,
    IdentityKind,
    create_identity_token,
)
from app.db.models import ApprovalRequest, NegotiationSession, Product
from app.main import create_app
from app.services.chat_service import BuyerOfferSubmission, ChatService
from app.services.pricing_service import ShippingPayer
from app.workers.approval_processor import ApprovalProcessor
from tests.fakes import RoutingDecisionProvider, demo_negotiation_decision
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration

TEST_SETTINGS = Settings(
    _env_file=None,
    database_url="mysql+pymysql://test:test@127.0.0.1/test",
    auth_secret="a-secure-test-secret-with-32-characters",
)


class SuccessfulFollowupProvider:
    def draft(self, request: ApprovalFollowupRequest) -> ApprovalFollowupDraft:
        return ApprovalFollowupDraft(
            acknowledged_event=request.event,
            reason="端到端测试按数据库审批事实生成通知",
            reply="候选文案不会直接发送",
        )


def _client(session_factory: sessionmaker[Session]) -> TestClient:
    application = create_app()
    application.dependency_overrides[get_session_factory_dependency] = (
        lambda: session_factory
    )
    application.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    return TestClient(application)


def _authenticate(client: TestClient, *, subject: str, kind: IdentityKind) -> None:
    assert TEST_SETTINGS.auth_secret is not None
    token = create_identity_token(
        subject=subject,
        kind=kind,
        secret=TEST_SETTINGS.auth_secret.get_secret_value(),
        lifetime=timedelta(minutes=30),
    )
    cookie = BUYER_SESSION_COOKIE if kind == "buyer" else SELLER_SESSION_COOKIE
    client.cookies.set(cookie, token)


def test_complete_v2_approval_and_intent_flow(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    turn = ChatService(
        service_session_factory,
        RoutingDecisionProvider(demo_negotiation_decision),
    ).send_buyer_message(
        session_id=session_id,
        buyer_id=buyer_id,
        request_id="v2-e2e-buyer-offer-001",
        content="2800 元、买家承担运费，可以吗？",
        offer=BuyerOfferSubmission(
            price=Decimal("2800.00"),
            shipping_paid_by=ShippingPayer.BUYER,
            shipping_cost=None,
            delivery_method="shipping",
        ),
    )
    assert turn.outcome == AgentTurnOutcome.NEEDS_SELLER_CONFIRMATION.value

    with service_session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        approval = db.scalar(
            select(ApprovalRequest).where(ApprovalRequest.session_id == session_id)
        )
        assert negotiation is not None
        assert approval is not None
        product = db.get(Product, negotiation.product_id)
        assert product is not None
        seller_id = product.seller_id
        approval_id = approval.id
        offer_id = approval.offer_id

    seller = _client(service_session_factory)
    buyer = _client(service_session_factory)
    _authenticate(seller, subject=seller_id, kind="seller")
    _authenticate(buyer, subject=buyer_id, kind="buyer")

    pending = seller.get(f"/api/seller/negotiations/{session_id}")
    approved = seller.post(
        f"/api/seller/approvals/{approval_id}/approve",
        json={"request_id": "v2-e2e-approve-001", "comment": "同意此报价"},
    )
    assert pending.status_code == 200
    assert pending.json()["negotiation"]["status"] == "WAITING_APPROVAL"
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["session_status"] == "WAITING_APPROVAL"

    processed = ApprovalProcessor(
        service_session_factory,
        SuccessfulFollowupProvider(),
    ).process_next()
    assert processed is not None
    assert processed.message_id is not None

    before_confirmation = seller.get(f"/api/seller/negotiations/{session_id}")
    assert before_confirmation.status_code == 200
    assert before_confirmation.json()["negotiation"]["status"] == "ACTIVE"
    assert before_confirmation.json()["negotiation"]["confirmed_offer_id"] is None

    confirmed = buyer.post(
        f"/api/negotiations/{session_id}/confirm",
        json={
            "offer_id": offer_id,
            "request_id": "v2-e2e-confirm-001",
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "AGREED"
    assert confirmed.json()["confirmation_source"] == "SELLER_APPROVED_BUYER_OFFER"

    completed = seller.get(f"/api/seller/negotiations/{session_id}")
    assert completed.status_code == 200
    payload = completed.json()
    assert payload["negotiation"]["status"] == "AGREED"
    assert payload["negotiation"]["confirmed_offer_id"] == offer_id
    assert payload["negotiation"]["latest_approval"]["followup_status"] == "SENT"
    assert payload["messages"][-1]["role"] == "SYSTEM"
    assert "实际成交" in payload["messages"][-1]["content"]
