from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.tools import AgentToolContext, build_seller_tools
from app.db.models import ApprovalRequest, ApprovalStatus, NegotiationSession, NegotiationStatus
from app.services.approval_service import ApprovalService
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import OfferTerms, ShippingPayer
from app.services.product_service import ProductService
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration


def test_tools_bind_identity_outside_model_visible_arguments(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    tools = build_seller_tools(
        context=AgentToolContext(session_id=session_id, buyer_id=buyer_id),
        product_service=ProductService(service_session_factory),
        negotiation_service=NegotiationService(service_session_factory),
        approval_service=ApprovalService(service_session_factory),
    )
    tools_by_name = {item.name: item for item in tools}

    assert set(tools_by_name) == {
        "get_product_info",
        "get_negotiation_state",
        "evaluate_offer",
        "submit_counter_offer",
        "accept_offer",
        "request_approval",
    }
    for item in tools:
        assert "session_id" not in item.args
        assert "buyer_id" not in item.args
        assert "seller_id" not in item.args

    product_result = tools_by_name["get_product_info"].invoke({})
    assert product_result["ok"] is True
    assert "minimum_net_price" not in str(product_result)

    approval_result = tools_by_name["evaluate_offer"].invoke(
        {"price": "2800.00", "shipping_paid_by": "buyer"}
    )
    assert approval_result == {
        "ok": True,
        "authorization": {
            "conditions_valid": True,
            "can_accept_automatically": False,
            "can_submit_counter_offer": False,
            "can_request_approval": True,
            "is_acceptance_prohibited": False,
            "reason_code": "SELLER_APPROVAL_REQUIRED",
        },
    }
    assert "2700" not in str(approval_result)
    assert "2850" not in str(approval_result)


def test_mutating_tool_rejects_unauthorized_counter_without_database_write(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    tools = build_seller_tools(
        context=AgentToolContext(session_id=session_id, buyer_id=buyer_id),
        product_service=ProductService(service_session_factory),
        negotiation_service=NegotiationService(service_session_factory),
        approval_service=ApprovalService(service_session_factory),
    )
    tools_by_name = {item.name: item for item in tools}

    rejected = tools_by_name["submit_counter_offer"].invoke(
        {"price": "2800.00", "shipping_paid_by": "buyer"}
    )
    accepted = tools_by_name["submit_counter_offer"].invoke(
        {
            "price": "2850.00",
            "shipping_paid_by": "buyer",
            "additional_terms": {"delivery_method": "pickup"},
        }
    )
    state = tools_by_name["get_negotiation_state"].invoke({})

    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "OFFER_NOT_AUTHORIZED"
    assert accepted["ok"] is True
    assert accepted["offer"]["price"] == str(Decimal("2850.00"))
    assert len(state["negotiation"]["recent_offers"]) == 1


def test_accept_offer_tool_accepts_current_authorized_buyer_offer(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    negotiation_service = NegotiationService(service_session_factory)
    buyer_offer = negotiation_service.record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=OfferTerms(
            buyer_payment=Decimal("2900.00"),
            shipping_paid_by=ShippingPayer.BUYER,
        ),
    )
    tools = build_seller_tools(
        context=AgentToolContext(session_id=session_id, buyer_id=buyer_id),
        product_service=ProductService(service_session_factory),
        negotiation_service=negotiation_service,
        approval_service=ApprovalService(service_session_factory),
    )
    accept_tool = next(item for item in tools if item.name == "accept_offer")

    result = accept_tool.invoke({"offer_id": buyer_offer.id})

    assert result["ok"] is True
    assert result["offer"]["id"] == buyer_offer.id
    assert result["offer"]["status"] == "ACCEPTED"


def test_request_approval_tool_is_bound_to_current_turn_and_idempotent(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    negotiation_service = NegotiationService(service_session_factory)
    buyer_offer = negotiation_service.record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=OfferTerms(
            buyer_payment=Decimal("2800.00"),
            shipping_paid_by=ShippingPayer.BUYER,
        ),
    )
    tools = build_seller_tools(
        context=AgentToolContext(
            session_id=session_id,
            buyer_id=buyer_id,
            current_turn_offer_id=buyer_offer.id,
        ),
        product_service=ProductService(service_session_factory),
        negotiation_service=negotiation_service,
        approval_service=ApprovalService(service_session_factory),
    )
    request_approval = next(item for item in tools if item.name == "request_approval")

    first = request_approval.invoke(
        {"offer_id": buyer_offer.id, "reason": "需要卖家确认"}
    )
    repeated = request_approval.invoke(
        {"offer_id": buyer_offer.id, "reason": "重复申请不会新增记录"}
    )

    assert first["ok"] is True
    assert repeated["ok"] is True
    assert repeated["approval"]["id"] == first["approval"]["id"]
    assert first["approval"]["status"] == "PENDING"
    with service_session_factory() as db:
        count = db.scalar(
            select(func.count())
            .select_from(ApprovalRequest)
            .where(ApprovalRequest.session_id == session_id)
        )
        approval = db.get(ApprovalRequest, first["approval"]["id"])
        negotiation = db.get(NegotiationSession, session_id)
        assert count == 1
        assert approval is not None
        assert approval.status is ApprovalStatus.PENDING
        assert negotiation is not None
        assert negotiation.status is NegotiationStatus.WAITING_APPROVAL


def test_request_approval_tool_rejects_offer_outside_bound_turn(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    negotiation_service = NegotiationService(service_session_factory)
    buyer_offer = negotiation_service.record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=OfferTerms(
            buyer_payment=Decimal("2800.00"),
            shipping_paid_by=ShippingPayer.BUYER,
        ),
    )
    tools = build_seller_tools(
        context=AgentToolContext(
            session_id=session_id,
            buyer_id=buyer_id,
            current_turn_offer_id=buyer_offer.id,
        ),
        product_service=ProductService(service_session_factory),
        negotiation_service=negotiation_service,
        approval_service=ApprovalService(service_session_factory),
    )
    request_approval = next(item for item in tools if item.name == "request_approval")

    result = request_approval.invoke(
        {"offer_id": buyer_offer.id + 1, "reason": "伪造报价编号"}
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "APPROVAL_NOT_AUTHORIZED"
    with service_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ApprovalRequest)) == 0
