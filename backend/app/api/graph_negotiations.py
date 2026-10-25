"""生产协商入口：只提交版本化图输入，停用时拒绝新会话。"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.agent.tools.serialization import negotiation_state_result, product_result
from app.api.dependencies import CurrentUser, SessionFactoryDependency, SettingsDependency
from app.db.models import Message, ModelExecutionTask
from app.db.models.graph import NegotiationInput
from app.runtime.contracts import Conflict
from app.runtime.input_queue import InputQueue, owned_session
from app.schemas.negotiation import (
    CloseNegotiationRequest,
    ConfirmNegotiationRequest,
    CreateNegotiationRequest,
    CreateNegotiationResponse,
    MessageListResponse,
    MessageResponse,
)
from app.semantic.contracts import QUALITY_VERSION, SEMANTIC_VERSIONS, ChatInput, ChatRequest
from app.services.errors import NegotiationNotFoundError, ServiceError
from app.services.negotiation_service import NegotiationService
from app.services.product_service import ProductService

router = APIRouter(prefix="/negotiations", tags=["negotiations"])


def require_graph(db, sid, buyer):
    try:
        session = owned_session(db, sid, buyer)
    except Conflict as exc:
        raise HTTPException(404, str(exc)) from exc
    if session.graph_version not in SEMANTIC_VERSIONS:
        raise HTTPException(409, "该会话需要原版本服务处理，不能切换协商运行时")
    return session


@router.post("", response_model=CreateNegotiationResponse)
def create_negotiation(
    payload: CreateNegotiationRequest,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
    settings: SettingsDependency,
):
    if not settings.graph_chat_enabled:
        raise HTTPException(503, "暂时停止创建协商会话")
    try:
        sid, created = NegotiationService(session_factory).create_or_get_active_session(
            product_id=payload.product_id, buyer_id=user.id, graph_version=QUALITY_VERSION
        )
    except ServiceError as exc:
        raise HTTPException(400, str(exc)) from exc
    with session_factory() as db:
        require_graph(db, sid, user.id)
    return CreateNegotiationResponse(session_id=sid, created=created)


@router.get("/{session_id}")
def get_negotiation(session_id: int, user: CurrentUser, session_factory: SessionFactoryDependency):
    with session_factory() as db:
        version = require_graph(db, session_id, user.id).graph_version
    try:
        product = ProductService(session_factory).get_for_negotiation(
            session_id=session_id, buyer_id=user.id
        )
        state = NegotiationService(session_factory).get_state(
            session_id=session_id, buyer_id=user.id
        )
    except NegotiationNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    body = negotiation_state_result(state)["negotiation"]
    for offer in body["recent_offers"]:
        offer.pop("shipping_cost", None)
        offer.pop("seller_borne_discount", None)
    return {
        "product": product_result(product)["product"],
        "negotiation": body,
        "graph_version": version,
    }


@router.get("/{session_id}/messages", response_model=MessageListResponse)
def get_messages(
    session_id: int,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
):
    with session_factory() as db:
        require_graph(db, session_id, user.id)
        messages = db.scalars(
            select(Message)
            .where(Message.session_id == session_id, Message.id > after_id)
            .order_by(Message.id)
            .limit(limit)
        )
        return MessageListResponse(messages=[MessageResponse.model_validate(m) for m in messages])


def enqueue(factory, sid, buyer, request_id, payload):
    with factory() as db:
        require_graph(db, sid, buyer)
        old = db.scalar(
            select(NegotiationInput.id).where(
                NegotiationInput.session_id == sid, NegotiationInput.request_id == request_id
            )
        )
    try:
        queue = InputQueue(factory)
        run_id = queue.enqueue(sid, buyer, request_id, payload)
        run = queue.read(run_id)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    with factory() as db:
        task_status = db.get(ModelExecutionTask, run["task_id"]).status.value
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


@router.post("/{session_id}/messages", status_code=202)
def send_message(
    session_id: int,
    payload: ChatRequest,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
):
    return enqueue(session_factory, session_id, user.id, payload.request_id, payload.as_input())


@router.post("/{session_id}/confirm", status_code=202)
def confirm(
    session_id: int,
    payload: ConfirmNegotiationRequest,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
):
    try:
        chat = ChatInput(
            text="确认交易意向",
            action="CONFIRM",
            offer_id=payload.offer_id,
            client_view_id=payload.client_view_id or "",
            display_receipt_id=payload.display_receipt_id,
        )
    except ValueError as exc:
        raise HTTPException(409, "确认需要该视图已展示报价的回执") from exc
    return enqueue(session_factory, session_id, user.id, payload.request_id, chat)


@router.post("/{session_id}/close", status_code=202)
def close(
    session_id: int,
    payload: CloseNegotiationRequest,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
):
    return enqueue(
        session_factory,
        session_id,
        user.id,
        payload.request_id,
        ChatInput(
            text="结束协商", action="CLOSE", client_view_id=payload.client_view_id or "button"
        ),
    )
