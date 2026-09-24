from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.agent.tools import AgentToolContext, build_seller_tools
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
    )
    tools_by_name = {item.name: item for item in tools}

    assert set(tools_by_name) == {
        "get_product_info",
        "get_negotiation_state",
        "evaluate_offer",
        "submit_counter_offer",
        "accept_offer",
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
    )
    tools_by_name = {item.name: item for item in tools}

    rejected = tools_by_name["submit_counter_offer"].invoke(
        {"price": "2800.00", "shipping_paid_by": "buyer"}
    )
    accepted = tools_by_name["submit_counter_offer"].invoke(
        {
            "price": "2850.00",
            "shipping_paid_by": "buyer",
            "additional_terms": {"delivery": "buyer_pickup"},
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
    )
    accept_tool = next(item for item in tools if item.name == "accept_offer")

    result = accept_tool.invoke({"offer_id": buyer_offer.id})

    assert result["ok"] is True
    assert result["offer"]["id"] == buyer_offer.id
    assert result["offer"]["status"] == "ACCEPTED"
