from typing import Annotated, Never

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from app.agent.decision_provider import DecisionProvider
from app.agent.tools.serialization import negotiation_state_result, product_result
from app.api.dependencies import (
    CurrentUser,
    SettingsDependency,
    get_decision_provider,
    get_session_factory_dependency,
)
from app.observability.service import ObservabilityEventInput, ObservabilityService
from app.runtime.contracts import Conflict
from app.runtime.input_queue import InputQueue, owned_session
from app.schemas.negotiation import (
    AcceptedInputResponse,
    CloseNegotiationRequest,
    CloseNegotiationResponse,
    ConfirmNegotiationRequest,
    ConfirmNegotiationResponse,
    CreateNegotiationRequest,
    CreateNegotiationResponse,
    MessageListResponse,
    MessageResponse,
    NegotiationDetailResponse,
    SendMessageRequest,
    SendMessageResponse,
)
from app.semantic.contracts import VERSION, ChatInput, ChatRequest
from app.services.chat_service import BuyerOfferSubmission, ChatService
from app.services.errors import (
    IncompleteRequestError,
    MessageConflictError,
    ModelDecisionError,
    ModelTaskRecoveryRequiredError,
    NegotiationLifecycleConflictError,
    NegotiationNotFoundError,
    ServiceError,
)
from app.services.intent_service import IntentService
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import PricingError
from app.services.product_service import ProductService

router = APIRouter(prefix="/negotiations", tags=["negotiations"])

SessionFactory = Annotated[
    sessionmaker[Session],
    Depends(get_session_factory_dependency),
]


@router.post(
    "",
    response_model=CreateNegotiationResponse,
    status_code=status.HTTP_200_OK,
)
def create_negotiation(
    payload: CreateNegotiationRequest,
    user: CurrentUser,
    session_factory: SessionFactory,
    settings: SettingsDependency,
) -> CreateNegotiationResponse:
    try:
        session_id, created = NegotiationService(session_factory).create_or_get_active_session(
            product_id=payload.product_id,
            buyer_id=user.id,
            graph_version=VERSION if settings.graph_chat_enabled else None,
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    return CreateNegotiationResponse(session_id=session_id, created=created)


@router.get("/{session_id}", response_model=NegotiationDetailResponse)
def get_negotiation(
    session_id: int,
    user: CurrentUser,
    session_factory: SessionFactory,
) -> NegotiationDetailResponse:
    try:
        product = ProductService(session_factory).get_for_negotiation(
            session_id=session_id,
            buyer_id=user.id,
        )
        negotiation = NegotiationService(session_factory).get_state(
            session_id=session_id,
            buyer_id=user.id,
        )
    except ServiceError as exc:
        _raise_http_error(exc)

    body = {
        "product": product_result(product)["product"],
        "negotiation": negotiation_state_result(negotiation)["negotiation"],
    }
    if is_semantic(session_factory, session_id, user.id):
        for offer in body["negotiation"]["recent_offers"]:
            offer.pop("shipping_cost", None)
            offer.pop("seller_borne_discount", None)
        return JSONResponse(content={**body, "graph_version": VERSION})
    return NegotiationDetailResponse.model_validate(body)


@router.post(
    "/{session_id}/confirm",
    response_model=ConfirmNegotiationResponse,
    responses={202: {"model": AcceptedInputResponse}},
)
def confirm_negotiation(
    session_id: int,
    payload: ConfirmNegotiationRequest,
    user: CurrentUser,
    session_factory: SessionFactory,
) -> ConfirmNegotiationResponse:
    if is_semantic(session_factory, session_id, user.id):
        try:
            return enqueue_chat(
                session_factory,
                session_id,
                user.id,
                payload.request_id,
                ChatInput(
                    text="确认交易意向",
                    action="CONFIRM",
                    offer_id=payload.offer_id,
                    client_view_id=payload.client_view_id or "",
                    display_receipt_id=payload.display_receipt_id,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="确认需要该视图已展示报价的回执") from exc
    try:
        result = IntentService(session_factory).confirm_offer(
            session_id=session_id,
            buyer_id=user.id,
            offer_id=payload.offer_id,
            request_id=payload.request_id,
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    ObservabilityService(session_factory).record(
        ObservabilityEventInput(
            event_type="NEGOTIATION_FINALIZED",
            action="CONFIRM_OFFER",
            outcome="SUCCESS",
            session_id=result.session_id,
            offer_id=result.confirmed_offer_id,
            attributes={
                "session_status": result.status.value,
                "confirmation_source": result.confirmation_source.value,
                "idempotent_replay": result.idempotent_replay,
            },
        )
    )
    return ConfirmNegotiationResponse.model_validate(result)


@router.post(
    "/{session_id}/close",
    response_model=CloseNegotiationResponse,
    responses={202: {"model": AcceptedInputResponse}},
)
def close_negotiation(
    session_id: int,
    payload: CloseNegotiationRequest,
    user: CurrentUser,
    session_factory: SessionFactory,
) -> CloseNegotiationResponse:
    if is_semantic(session_factory, session_id, user.id):
        return enqueue_chat(
            session_factory,
            session_id,
            user.id,
            payload.request_id,
            ChatInput(
                text="结束协商", action="CLOSE", client_view_id=payload.client_view_id or "button"
            ),
        )
    try:
        result = IntentService(session_factory).close_negotiation(
            session_id=session_id,
            buyer_id=user.id,
            request_id=payload.request_id,
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    ObservabilityService(session_factory).record(
        ObservabilityEventInput(
            event_type="NEGOTIATION_CLOSED",
            action="CLOSE_NEGOTIATION",
            outcome="SUCCESS",
            session_id=result.session_id,
            approval_id=result.cancelled_approval_id,
            attributes={
                "session_status": result.status.value,
                "idempotent_replay": result.idempotent_replay,
            },
        )
    )
    return CloseNegotiationResponse.model_validate(result)


@router.get("/{session_id}/messages", response_model=MessageListResponse)
def get_messages(
    session_id: int,
    user: CurrentUser,
    session_factory: SessionFactory,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> MessageListResponse:
    try:
        messages = ChatService(
            session_factory,
        ).list_messages(
            session_id=session_id,
            buyer_id=user.id,
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
    responses={202: {"model": AcceptedInputResponse}},
)
def send_message(
    session_id: int,
    payload: ChatRequest | SendMessageRequest,
    user: CurrentUser,
    session_factory: SessionFactory,
    decision_provider: Annotated[DecisionProvider, Depends(get_decision_provider)],
) -> SendMessageResponse:
    if isinstance(payload, ChatRequest):
        return enqueue_chat(
            session_factory, session_id, user.id, payload.request_id, payload.as_input()
        )
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
            buyer_id=user.id,
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


def is_semantic(factory, sid, buyer):
    try:
        with factory() as db:
            return owned_session(db, sid, buyer).graph_version == VERSION
    except Conflict as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def enqueue_chat(factory, sid, buyer, request_id, payload):
    from sqlalchemy import select

    from app.db.models import ModelExecutionTask
    from app.db.models.graph import NegotiationInput

    try:
        queue = InputQueue(factory)
        with factory() as db:
            old = db.scalar(
                select(NegotiationInput).where(
                    NegotiationInput.session_id == sid, NegotiationInput.request_id == request_id
                )
            )
        run_id = queue.enqueue(sid, buyer, request_id, payload)
        run = queue.read(run_id)
        with factory() as db:
            task = db.get(ModelExecutionTask, run["task_id"])
            task_status = task.status.value
        return JSONResponse(
            status_code=202,
            content={
                "run_id": run_id,
                "input_seq": run["input_seq"],
                "buyer_message_id": run["buyer_message_id"],
                "status": task_status,
                "idempotent_replay": old is not None,
            },
        )
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _raise_http_error(error: ServiceError | PricingError) -> Never:
    if isinstance(error, NegotiationNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, ModelDecisionError):
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif isinstance(
        error,
        (
            MessageConflictError,
            IncompleteRequestError,
            ModelTaskRecoveryRequiredError,
            NegotiationLifecycleConflictError,
        ),
    ):
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_400_BAD_REQUEST
    raise HTTPException(status_code=code, detail=str(error)) from error
