from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.approval_followup import (
    ApprovalFollowupDraft,
    ApprovalFollowupRequest,
)
from app.agent.seller_agent import AgentTurnOutcome
from app.api.dependencies import (
    get_decision_provider,
    get_session_factory_dependency,
)
from app.core.config import Settings, get_settings
from app.db.models import ApprovalRequest, NegotiationSession, Product
from app.main import create_app
from app.services.chat_service import BuyerOfferSubmission, ChatService
from app.services.pricing_service import ShippingPayer
from app.workers.approval_processor import ApprovalProcessor
from tests.fakes import RoutingDecisionProvider, demo_negotiation_decision
from tests.integration.auth_helpers import authenticate_user
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


def _client(
    session_factory: sessionmaker[Session],
    *,
    with_fake_model: bool = False,
) -> TestClient:
    application = create_app()
    application.dependency_overrides[get_session_factory_dependency] = (
        lambda: session_factory
    )
    application.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    if with_fake_model:
        application.dependency_overrides[get_decision_provider] = lambda: (
            RoutingDecisionProvider(demo_negotiation_decision)
        )
    return TestClient(application)


def _register(
    client: TestClient,
    *,
    username: str,
    display_name: str,
) -> dict[str, object]:
    response = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "display_name": display_name,
            "password": "Strong-account-password-2026",
        },
    )
    assert response.status_code == 201
    return response.json()


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
    authenticate_user(seller, user_id=seller_id, settings=TEST_SETTINGS)
    authenticate_user(buyer, user_id=buyer_id, settings=TEST_SETTINGS)

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


def test_complete_v21_registered_account_flow(
    service_session_factory: sessionmaker[Session],
) -> None:
    """两个统一账号完成发布、浏览、显式协商、审批和意向确认闭环。"""

    seller = _client(service_session_factory)
    buyer = _client(service_session_factory, with_fake_model=True)
    stranger = _client(service_session_factory)
    seller_identity = _register(
        seller,
        username="v21-e2e-seller",
        display_name="阶段七卖家",
    )
    _register(
        buyer,
        username="v21-e2e-buyer",
        display_name="阶段七买家",
    )
    _register(
        stranger,
        username="v21-e2e-stranger",
        display_name="无关账号",
    )

    created_product = seller.post(
        "/api/products",
        json={
            "title": "阶段七端到端商品",
            "description": "验证统一账号与显式协商全流程。",
            "listed_price": "3000.00",
            "status": "AVAILABLE",
            "policy": {
                "minimum_net_price": "2700.00",
                "auto_accept_threshold": "2850.00",
                "negotiation_style": "BALANCED",
                "max_rounds": 6,
            },
        },
    )
    assert created_product.status_code == 201
    product_id = created_product.json()["id"]

    with service_session_factory() as db:
        before_browsing = db.scalar(
            select(func.count())
            .select_from(NegotiationSession)
            .where(NegotiationSession.product_id == product_id)
        )

    hall = buyer.get("/api/products")
    seller_page = buyer.get(f"/api/sellers/{seller_identity['id']}")
    seller_products = buyer.get(
        f"/api/sellers/{seller_identity['id']}/products"
    )
    detail = buyer.get(f"/api/products/{product_id}")
    refreshed = buyer.get(f"/api/products/{product_id}")
    assert hall.status_code == 200
    assert any(item["id"] == product_id for item in hall.json()["products"])
    assert seller_page.status_code == 200
    assert seller_page.json()["display_name"] == "阶段七卖家"
    assert seller_products.status_code == 200
    assert detail.status_code == 200
    assert refreshed.status_code == 200
    with service_session_factory() as db:
        after_browsing = db.scalar(
            select(func.count())
            .select_from(NegotiationSession)
            .where(NegotiationSession.product_id == product_id)
        )
    assert before_browsing == after_browsing == 0

    created_session = buyer.post(
        "/api/negotiations",
        json={"product_id": product_id},
    )
    restored_session = buyer.post(
        "/api/negotiations",
        json={"product_id": product_id},
    )
    own_product = seller.post(
        "/api/negotiations",
        json={"product_id": product_id},
    )
    assert created_session.status_code == 200
    assert created_session.json()["created"] is True
    assert restored_session.status_code == 200
    assert restored_session.json() == {
        "session_id": created_session.json()["session_id"],
        "created": False,
    }
    assert own_product.status_code == 409
    session_id = created_session.json()["session_id"]

    # 同一 Cookie 可进入买家和卖家资源，模式不是固定权限角色。
    assert seller.get("/api/buyer/negotiations").status_code == 200
    assert buyer.get("/api/seller/products").status_code == 200
    assert buyer.get("/api/buyer/negotiations").json()["negotiations"][0][
        "id"
    ] == session_id
    assert seller.get("/api/seller/negotiations").json()["negotiations"][0][
        "id"
    ] == session_id

    turn = buyer.post(
        f"/api/negotiations/{session_id}/messages",
        json={
            "request_id": "v21-e2e-offer-001",
            "content": "2800 元、买家承担运费，可以吗？",
            "offer": {
                "price": "2800.00",
                "shipping_paid_by": "buyer",
                "shipping_cost": None,
                "delivery_method": "shipping",
            },
        },
    )
    assert turn.status_code == 201
    assert turn.json()["outcome"] == "NEEDS_SELLER_CONFIRMATION"

    with service_session_factory() as db:
        approval = db.scalar(
            select(ApprovalRequest).where(ApprovalRequest.session_id == session_id)
        )
        assert approval is not None
        approval_id = approval.id
        offer_id = approval.offer_id

    assert stranger.get(f"/api/negotiations/{session_id}").status_code == 404
    assert (
        stranger.get(f"/api/seller/negotiations/{session_id}").status_code
        == 404
    )
    assert stranger.get(f"/api/seller/products/{product_id}").status_code == 404

    approved = seller.post(
        f"/api/seller/approvals/{approval_id}/approve",
        json={"request_id": "v21-e2e-approve-001", "comment": "同意报价"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"

    processed = ApprovalProcessor(
        service_session_factory,
        SuccessfulFollowupProvider(),
    ).process_next()
    assert processed is not None
    assert processed.message_id is not None

    confirmed = buyer.post(
        f"/api/negotiations/{session_id}/confirm",
        json={
            "offer_id": offer_id,
            "request_id": "v21-e2e-confirm-001",
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "AGREED"
    assert confirmed.json()["confirmation_source"] == (
        "SELLER_APPROVED_BUYER_OFFER"
    )
    completed = seller.get(f"/api/seller/negotiations/{session_id}")
    assert completed.status_code == 200
    assert completed.json()["negotiation"]["status"] == "AGREED"
    assert completed.json()["negotiation"]["buyer_display_name"] == "阶段七买家"
