from app.services.errors import ServiceError
from app.services.negotiation_service import (
    NegotiationState,
    OfferAuthorization,
    OfferSnapshot,
)
from app.services.pricing_service import PricingError
from app.services.product_service import ProductInfo


def error_result(error: ServiceError | PricingError) -> dict[str, object]:
    code = error.code if isinstance(error, ServiceError) else "INVALID_OFFER_TERMS"
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": str(error),
        },
    }


def product_result(product: ProductInfo) -> dict[str, object]:
    return {
        "ok": True,
        "product": {
            "id": product.id,
            "title": product.title,
            "description": product.description,
            "listed_price": str(product.listed_price),
            "status": product.status.value,
        },
    }


def authorization_result(authorization: OfferAuthorization) -> dict[str, object]:
    # 不返回最低价或自动接受阈值，只向 Agent 暴露可执行权限。
    return {
        "ok": True,
        "authorization": {
            "can_accept_automatically": authorization.can_accept_automatically,
            "can_submit_counter_offer": authorization.can_submit_counter_offer,
            "can_request_approval": authorization.can_request_approval,
            "is_acceptance_prohibited": authorization.is_acceptance_prohibited,
            "reason_code": authorization.reason_code,
        },
    }


def offer_result(offer: OfferSnapshot) -> dict[str, object]:
    return {
        "ok": True,
        "offer": serialize_offer(offer),
    }


def negotiation_state_result(state: NegotiationState) -> dict[str, object]:
    return {
        "ok": True,
        "negotiation": {
            "id": state.id,
            "product_id": state.product_id,
            "status": state.status.value,
            "current_offer_id": state.current_offer_id,
            "round_count": state.round_count,
            "version": state.version,
            "negotiation_style": state.negotiation_style.value,
            "max_rounds": state.max_rounds,
            "recent_offers": [serialize_offer(offer) for offer in state.recent_offers],
        },
    }


def serialize_offer(offer: OfferSnapshot) -> dict[str, object]:
    return {
        "id": offer.id,
        "proposer": offer.proposer.value,
        "price": str(offer.price),
        "shipping_paid_by": offer.shipping_paid_by.value,
        "shipping_cost": str(offer.shipping_cost) if offer.shipping_cost is not None else None,
        "seller_borne_discount": str(offer.seller_borne_discount),
        "additional_terms": offer.additional_terms,
        "status": offer.status.value,
        "expires_at": offer.expires_at.isoformat() if offer.expires_at is not None else None,
        "created_at": offer.created_at.isoformat(),
    }
