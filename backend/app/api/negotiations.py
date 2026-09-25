from typing import Annotated, Never

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, sessionmaker

from app.agent.decision_provider import DecisionProvider
from app.agent.tools.serialization import negotiation_state_result, product_result
from app.api.dependencies import (
    get_current_buyer_id,
    get_decision_provider,
    get_session_factory_dependency,
)
from app.schemas.negotiation import (
    CreateNegotiationRequest,
    CreateNegotiationResponse,
    MessageListResponse,
    MessageResponse,
    NegotiationDetailResponse,
    SendMessageRequest,
    SendMessageResponse,
)
from app.services.chat_service import BuyerOfferSubmission, ChatService
from app.services.errors import (
    IncompleteRequestError,
    MessageConflictError,
    NegotiationNotFoundError,
    ServiceError,
)
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import PricingError
from app.services.product_service import ProductService

router = APIRouter(prefix="/negotiations", tags=["negotiations"])

SessionFactory = Annotated[
    sessionmaker[Session],
    Depends(get_session_factory_dependency),
]
BuyerId = Annotated[
    str,
    Depends(get_current_buyer_id),
]


@router.post(
    "",
    response_model=CreateNegotiationResponse,
    status_code=status.HTTP_200_OK,
)
def create_negotiation(
    payload: CreateNegotiationRequest,
    buyer_id: BuyerId,
    session_factory: SessionFactory,
) -> CreateNegotiationResponse:
    try:
        session_id, created = NegotiationService(
            session_factory
        ).create_or_get_active_session(
            product_id=payload.product_id,
            buyer_id=buyer_id,
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    return CreateNegotiationResponse(session_id=session_id, created=created)


@router.get("/{session_id}", response_model=NegotiationDetailResponse)
def get_negotiation(
    session_id: int,
    buyer_id: BuyerId,
    session_factory: SessionFactory,
) -> NegotiationDetailResponse:
    try:
        product = ProductService(session_factory).get_for_negotiation(
            session_id=session_id,
            buyer_id=buyer_id,
        )
        negotiation = NegotiationService(session_factory).get_state(
            session_id=session_id,
            buyer_id=buyer_id,
        )
    except ServiceError as exc:
        _raise_http_error(exc)

    return NegotiationDetailResponse.model_validate(
        {
            "product": product_result(product)["product"],
            "negotiation": negotiation_state_result(negotiation)["negotiation"],
        }
    )


@router.get("/{session_id}/messages", response_model=MessageListResponse)
def get_messages(
    session_id: int,
    buyer_id: BuyerId,
    session_factory: SessionFactory,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> MessageListResponse:
    try:
        messages = ChatService(
            session_factory,
        ).list_messages(
            session_id=session_id,
            buyer_id=buyer_id,
            after_id=after_id,
            limit=limit,
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    return MessageListResponse(
        messages=[MessageResponse.model_validate(message) for message in messages]
    )


@router.post(
    "/{session_id}/messages",
    response_model=SendMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
def send_message(
    session_id: int,
    payload: SendMessageRequest,
    buyer_id: BuyerId,
    session_factory: SessionFactory,
    decision_provider: Annotated[DecisionProvider, Depends(get_decision_provider)],
) -> SendMessageResponse:
    offer = None
    if payload.offer is not None:
        offer = BuyerOfferSubmission(
            price=payload.offer.price,
            shipping_paid_by=payload.offer.shipping_paid_by,
            shipping_cost=payload.offer.shipping_cost,
            delivery_method=payload.offer.delivery_method,
        )
    try:
        result = ChatService(session_factory, decision_provider).send_buyer_message(
            session_id=session_id,
            buyer_id=buyer_id,
            request_id=payload.request_id,
            content=payload.content,
            offer=offer,
        )
    except (ServiceError, PricingError) as exc:
        _raise_http_error(exc)

    return SendMessageResponse(
        buyer_message=MessageResponse.model_validate(result.buyer_message),
        agent_message=MessageResponse.model_validate(result.agent_message),
        outcome=result.outcome,
        formal_offer_id=result.formal_offer_id,
        idempotent_replay=result.idempotent_replay,
    )


def _raise_http_error(error: ServiceError | PricingError) -> Never:
    if isinstance(error, NegotiationNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, (MessageConflictError, IncompleteRequestError)):
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_400_BAD_REQUEST
    raise HTTPException(status_code=code, detail=str(error)) from error
