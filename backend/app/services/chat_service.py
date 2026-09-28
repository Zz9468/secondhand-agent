import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.decision import NegotiationDecision
from app.agent.decision_provider import (
    ConversationMessage,
    DecisionProvider,
    DecisionRequest,
)
from app.agent.seller_agent import (
    AgentTurnOutcome,
    AgentTurnPreparation,
    AgentTurnResult,
    SellerAgent,
)
from app.agent.tools import AgentToolContext, build_seller_tools
from app.core.config import get_settings
from app.db.models import (
    Message,
    MessageRole,
    ModelExecutionTask,
    ModelTaskErrorCategory,
    ModelTaskStatus,
    ModelTaskType,
    NegotiationSession,
    NegotiationStatus,
    Offer,
    Product,
    SellerPolicy,
)
from app.services.approval_service import ApprovalService
from app.services.errors import (
    IncompleteRequestError,
    MessageConflictError,
    ModelDecisionError,
    ModelExecutionTaskLeaseError,
    ModelTaskRecoveryRequiredError,
    NegotiationNotFoundError,
)
from app.services.model_retry_policy import ModelRetryPolicy
from app.services.model_task_service import (
    ModelExecutionTaskSnapshot,
    ModelTaskService,
    ModelUsage,
)
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import OfferTerms, ShippingPayer
from app.services.product_service import ProductService


@dataclass(frozen=True, slots=True)
class BuyerOfferSubmission:
    price: Decimal
    shipping_paid_by: ShippingPayer
    shipping_cost: Decimal | None = None
    delivery_method: str | None = None

    def to_terms(self) -> OfferTerms:
        return OfferTerms(
            buyer_payment=self.price,
            shipping_paid_by=self.shipping_paid_by,
            shipping_cost=self.shipping_cost,
        )

    def additional_terms(self) -> dict[str, object]:
        if self.delivery_method is None:
            return {}
        return {"delivery_method": self.delivery_method}


@dataclass(frozen=True, slots=True)
class MessageSnapshot:
    id: int
    role: MessageRole
    content: str
    request_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChatTurnSnapshot:
    buyer_message: MessageSnapshot
    agent_message: MessageSnapshot
    outcome: str
    formal_offer_id: int | None
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class ChatTaskProcessingResult:
    task_id: int
    status: ModelTaskStatus
    turn: ChatTurnSnapshot | None


@dataclass(frozen=True, slots=True)
class _PendingChatTurn:
    session_id: int
    buyer_snapshot: MessageSnapshot
    task_id: int
    idempotent_replay: bool


class ChatService:
    """用持久化任务把聊天模型调用隔离在数据库事务之外。"""

    _history_limit = 20

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        decision_provider: DecisionProvider | None = None,
        retry_policy: ModelRetryPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._decision_provider = decision_provider
        self._negotiation_service = NegotiationService(session_factory)
        self._approval_service = ApprovalService(session_factory)
        self._task_service = ModelTaskService(session_factory)
        self._retry_policy = retry_policy or ModelRetryPolicy.from_settings(
            get_settings()
        )

    def send_buyer_message(
        self,
        *,
        session_id: int,
        buyer_id: str,
        request_id: str,
        content: str,
        offer: BuyerOfferSubmission | None = None,
    ) -> ChatTurnSnapshot:
        if self._decision_provider is None:
            raise RuntimeError("发送消息前必须配置决策模型")

        prepared = self._prepare_turn(
            session_id=session_id,
            buyer_id=buyer_id,
            request_id=request_id,
            content=content,
            offer=offer,
        )
        if isinstance(prepared, ChatTurnSnapshot):
            return prepared
        return self._execute_pending_turn(prepared)

    def list_messages(
        self,
        *,
        session_id: int,
        buyer_id: str,
        after_id: int = 0,
        limit: int = 100,
    ) -> tuple[MessageSnapshot, ...]:
        with self._session_factory() as db:
            self._require_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
            )
            messages = db.scalars(
                select(Message)
                .where(
                    Message.session_id == session_id,
                    Message.id > after_id,
                )
                .order_by(Message.id)
                .limit(limit)
            )
            return tuple(self._snapshot(message) for message in messages)

    def _prepare_turn(
        self,
        *,
        session_id: int,
        buyer_id: str,
        request_id: str,
        content: str,
        offer: BuyerOfferSubmission | None,
    ) -> ChatTurnSnapshot | _PendingChatTurn:
        reply_request_id = self._reply_request_id(request_id)
        request_fingerprint = self._request_fingerprint(content=content, offer=offer)

        with self._session_factory() as db, db.begin():
            negotiation = self._require_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
                for_update=True,
            )
            existing_buyer = self._message_by_request_id(
                db,
                session_id=session_id,
                request_id=request_id,
            )
            if existing_buyer is not None:
                return self._resume_existing(
                    db,
                    existing_buyer=existing_buyer,
                    reply_request_id=reply_request_id,
                    expected_fingerprint=request_fingerprint,
                )
            if negotiation.status in {
                NegotiationStatus.AGREED,
                NegotiationStatus.CLOSED,
            }:
                raise MessageConflictError("当前会话已经结束，不能继续发送消息")
            if self._has_active_chat_task(db, session_id=session_id):
                raise IncompleteRequestError("当前会话仍有一条消息正在处理，请稍后重试")

            history = self._recent_history(db, session_id=session_id)
            buyer_offer_id: int | None = None
            if offer is not None:
                if negotiation.status is NegotiationStatus.WAITING_APPROVAL:
                    self._approval_service.cancel_pending_for_new_offer_in_transaction(
                        db=db,
                        session_id=session_id,
                        buyer_id=buyer_id,
                    )
                buyer_offer = self._negotiation_service.record_buyer_offer_in_transaction(
                    db=db,
                    session_id=session_id,
                    buyer_id=buyer_id,
                    terms=offer.to_terms(),
                    additional_terms=offer.additional_terms(),
                )
                buyer_offer_id = buyer_offer.id

            buyer_message = Message(
                session_id=session_id,
                role=MessageRole.BUYER,
                content=content,
                request_id=request_id,
                request_fingerprint=request_fingerprint,
            )
            db.add(buyer_message)
            db.flush()
            db.refresh(buyer_message)
            buyer_snapshot = self._snapshot(buyer_message)

            turn_session_factory = self._transaction_session_factory(db)
            agent = self._build_agent(
                session_id=session_id,
                buyer_id=buyer_id,
                session_factory=turn_session_factory,
                current_turn_offer_id=buyer_offer_id,
            )
            preparation = agent.prepare_turn(
                content,
                conversation_history=history,
                current_turn_offer_id=buyer_offer_id,
            )
            if not preparation.requires_model:
                result = agent.apply_prepared(preparation)
                return self._persist_result(
                    db,
                    session_id=session_id,
                    buyer_snapshot=buyer_snapshot,
                    reply_request_id=reply_request_id,
                    result=result,
                    idempotent_replay=False,
                )

            decision_request = preparation.decision_request
            if decision_request is None:  # pragma: no cover - 防御分支
                raise RuntimeError("模型任务缺少决策输入")
            current_offer_id = self._context_current_offer_id(decision_request)
            creation = self._task_service.create_task_in_transaction(
                db=db,
                task_type=ModelTaskType.CHAT_DECISION,
                business_key=self._task_business_key(session_id, request_id),
                session_id=session_id,
                offer_id=current_offer_id,
                input_snapshot=self._serialize_task_input(
                    buyer_message_id=buyer_message.id,
                    reply_request_id=reply_request_id,
                    decision_request=decision_request,
                ),
                max_attempts=self._retry_policy.max_attempts,
            )
            return _PendingChatTurn(
                session_id=session_id,
                buyer_snapshot=buyer_snapshot,
                task_id=creation.task.id,
                idempotent_replay=False,
            )

    def _execute_pending_turn(self, pending: _PendingChatTurn) -> ChatTurnSnapshot:
        leased = self._task_service.lease_task(
            task_id=pending.task_id,
            worker_id=f"chat-sync:{pending.task_id}",
            lease_seconds=self._retry_policy.lease_seconds,
        )
        if leased is None:
            replay = self._load_reply(
                session_id=pending.session_id,
                buyer_snapshot=pending.buyer_snapshot,
                idempotent_replay=True,
            )
            if replay is not None:
                return replay
            task = self._task_service.get_task(task_id=pending.task_id)
            if task.status is ModelTaskStatus.RUNNING:
                raise IncompleteRequestError("该请求正在处理中，请稍后查询消息记录")
            if task.status is ModelTaskStatus.RETRY_WAIT:
                raise ModelDecisionError("模型暂时无法完成本轮决策，请稍后重试")
            if task.status in {ModelTaskStatus.FAILED, ModelTaskStatus.CANCELLED}:
                raise ModelTaskRecoveryRequiredError(
                    "该模型任务已停止自动执行，需要卖家人工重试或终止"
                )
            raise IncompleteRequestError("该请求尚未形成完整回复，请稍后查询消息记录")

        if leased.status is ModelTaskStatus.FAILED:
            raise ModelTaskRecoveryRequiredError(
                "该模型任务已达到最大尝试次数，需要卖家人工处理"
            )
        return self._execute_leased_turn(pending=pending, leased=leased)

    def process_next_pending_task(
        self,
        *,
        worker_id: str,
    ) -> ChatTaskProcessingResult | None:
        """由恢复 Worker 处理一个到期聊天任务，包括过期租约接管。"""

        leased = self._task_service.lease_next(
            worker_id=worker_id,
            task_types=(ModelTaskType.CHAT_DECISION,),
            lease_seconds=self._retry_policy.lease_seconds,
        )
        if leased is None:
            return None
        if leased.status is ModelTaskStatus.FAILED:
            return ChatTaskProcessingResult(
                task_id=leased.id,
                status=leased.status,
                turn=None,
            )
        try:
            pending = self._pending_from_task(leased)
        except IncompleteRequestError:
            self._task_service.fail_task(
                task_id=leased.id,
                lease_token=self._required_lease_token(leased),
                error_category=ModelTaskErrorCategory.INVALID_OUTPUT,
                error_message="聊天模型任务输入快照无效",
            )
            return ChatTaskProcessingResult(
                task_id=leased.id,
                status=ModelTaskStatus.FAILED,
                turn=None,
            )
        try:
            turn = self._execute_leased_turn(pending=pending, leased=leased)
        except (
            IncompleteRequestError,
            ModelDecisionError,
            ModelTaskRecoveryRequiredError,
        ):
            task = self._task_service.get_task(task_id=leased.id)
            return ChatTaskProcessingResult(
                task_id=task.id,
                status=task.status,
                turn=None,
            )
        current = self._task_service.get_task(task_id=leased.id)
        return ChatTaskProcessingResult(
            task_id=leased.id,
            status=current.status,
            turn=turn,
        )

    def _execute_leased_turn(
        self,
        *,
        pending: _PendingChatTurn,
        leased: ModelExecutionTaskSnapshot,
    ) -> ChatTurnSnapshot:
        lease_token = self._required_lease_token(leased)
        try:
            replay = self._reconcile_existing_reply(
                pending=pending,
                leased=leased,
                lease_token=lease_token,
            )
            if replay is not None:
                return replay
            request = self._deserialize_decision_request(leased.input_snapshot)
        except ModelExecutionTaskLeaseError as exc:
            replay = self._load_reply(
                session_id=pending.session_id,
                buyer_snapshot=pending.buyer_snapshot,
                idempotent_replay=True,
            )
            if replay is not None:
                return replay
            raise IncompleteRequestError(
                "模型任务租约已变化，请稍后查询消息记录"
            ) from exc
        except IncompleteRequestError as exc:
            self._task_service.fail_task(
                task_id=leased.id,
                lease_token=lease_token,
                error_category=ModelTaskErrorCategory.INVALID_OUTPUT,
                error_message=str(exc),
            )
            raise ModelTaskRecoveryRequiredError(
                "模型任务持久化输入无效，需要卖家人工处理"
            ) from exc
        except MessageConflictError as exc:
            self._task_service.fail_task(
                task_id=leased.id,
                lease_token=lease_token,
                error_category=ModelTaskErrorCategory.BUSINESS_CONFLICT,
                error_message=str(exc),
            )
            raise ModelTaskRecoveryRequiredError(
                "模型任务幂等结果冲突，需要卖家人工处理"
            ) from exc
        try:
            provider = self._decision_provider
            if provider is None:  # pragma: no cover - 入口已校验
                raise RuntimeError("发送消息前必须配置决策模型")
            decision = provider.decide(request)
        except Exception as exc:
            plan = self._retry_policy.plan_failure(
                task_id=leased.id,
                attempt_count=leased.attempt_count,
                max_attempts=leased.max_attempts,
                error=exc,
            )
            if plan.should_retry and plan.next_retry_at is not None:
                self._task_service.defer_retry(
                    task_id=leased.id,
                    lease_token=lease_token,
                    error_category=plan.category,
                    error_message=plan.safe_message,
                    next_retry_at=plan.next_retry_at,
                )
                raise ModelDecisionError(
                    "模型暂时无法完成本轮决策，任务已按退避策略等待重试"
                ) from exc
            self._task_service.fail_task(
                task_id=leased.id,
                lease_token=lease_token,
                error_category=plan.category,
                error_message=plan.safe_message,
            )
            raise ModelTaskRecoveryRequiredError(
                "模型任务已停止自动重试，需要卖家人工处理"
            ) from exc

        try:
            return self._finalize_model_turn(
                pending=pending,
                leased=leased,
                decision=decision,
            )
        except ModelExecutionTaskLeaseError as exc:
            replay = self._load_reply(
                session_id=pending.session_id,
                buyer_snapshot=pending.buyer_snapshot,
                idempotent_replay=True,
            )
            if replay is not None:
                return replay
            raise IncompleteRequestError(
                "模型任务租约已变化，本次迟到结果未写入，请稍后重试"
            ) from exc
        except MessageConflictError as exc:
            self._task_service.fail_task(
                task_id=leased.id,
                lease_token=lease_token,
                error_category=ModelTaskErrorCategory.BUSINESS_CONFLICT,
                error_message=str(exc),
            )
            raise ModelTaskRecoveryRequiredError(
                "模型任务幂等结果冲突，需要卖家人工处理"
            ) from exc

    def _reconcile_existing_reply(
        self,
        *,
        pending: _PendingChatTurn,
        leased: ModelExecutionTaskSnapshot,
        lease_token: str,
    ) -> ChatTurnSnapshot | None:
        """恢复“回复已落库、任务未收口”的部分成功，且不再调用模型。"""

        reply_request_id = self._task_reply_request_id(leased.input_snapshot)
        with self._session_factory() as db, db.begin():
            current_task = self._task_service.require_active_lease_in_transaction(
                db=db,
                task_id=leased.id,
                lease_token=lease_token,
            )
            existing_reply = self._message_by_request_id(
                db,
                session_id=current_task.session_id,
                request_id=reply_request_id,
            )
            if existing_reply is None:
                return None
            if existing_reply.role is not MessageRole.AGENT:
                raise MessageConflictError("模型任务回复幂等键已被其他消息占用")
            self._task_service.cancel_with_lease_in_transaction(
                db=db,
                task_id=current_task.id,
                lease_token=lease_token,
                error_message="等价 Agent 回复已经存在，恢复任务不再调用模型",
            )
            return self._turn_snapshot(
                buyer_snapshot=pending.buyer_snapshot,
                agent_message=existing_reply,
                idempotent_replay=True,
            )

    def _finalize_model_turn(
        self,
        *,
        pending: _PendingChatTurn,
        leased: ModelExecutionTaskSnapshot,
        decision: NegotiationDecision,
    ) -> ChatTurnSnapshot:
        lease_token = leased.lease_token
        if lease_token is None:  # pragma: no cover - 调用方只传入已领取任务
            raise RuntimeError("已领取模型任务缺少租约令牌")

        with self._session_factory() as db, db.begin():
            current_task = self._task_service.require_active_lease_in_transaction(
                db=db,
                task_id=leased.id,
                lease_token=lease_token,
            )
            negotiation = db.get(
                NegotiationSession,
                current_task.session_id,
                with_for_update=True,
            )
            stale_reason = self._stale_reason(
                db,
                task=current_task,
                negotiation=negotiation,
            )
            reply_request_id = self._task_reply_request_id(current_task.input_snapshot)
            existing_reply = self._message_by_request_id(
                db,
                session_id=current_task.session_id,
                request_id=reply_request_id,
            )
            if existing_reply is not None:
                if existing_reply.role is not MessageRole.AGENT:
                    raise MessageConflictError("模型任务回复幂等键已被其他消息占用")
                self._task_service.cancel_with_lease_in_transaction(
                    db=db,
                    task_id=current_task.id,
                    lease_token=lease_token,
                    error_message="等价 Agent 回复已经存在",
                )
                return self._turn_snapshot(
                    buyer_snapshot=pending.buyer_snapshot,
                    agent_message=existing_reply,
                    idempotent_replay=True,
                )

            if stale_reason is not None:
                result = AgentTurnResult(
                    reply="协商条件已经发生变化，本次模型结果未生效，请查看最新状态后重试。",
                    outcome=AgentTurnOutcome.SAFE_FAILURE,
                    decision=None,
                )
                snapshot = self._persist_result(
                    db,
                    session_id=current_task.session_id,
                    buyer_snapshot=pending.buyer_snapshot,
                    reply_request_id=reply_request_id,
                    result=result,
                    idempotent_replay=pending.idempotent_replay,
                )
                self._task_service.mark_stale_in_transaction(
                    db=db,
                    task_id=current_task.id,
                    lease_token=lease_token,
                    error_message=stale_reason,
                )
                return snapshot

            request = self._deserialize_decision_request(current_task.input_snapshot)
            preparation = AgentTurnPreparation(decision_request=request)
            turn_session_factory = self._transaction_session_factory(db)
            agent = self._build_agent(
                session_id=current_task.session_id,
                buyer_id=self._required_buyer_id(negotiation),
                session_factory=turn_session_factory,
                current_turn_offer_id=request.current_turn_offer_id,
            )
            result = agent.apply_prepared(preparation, decision=decision)
            snapshot = self._persist_result(
                db,
                session_id=current_task.session_id,
                buyer_snapshot=pending.buyer_snapshot,
                reply_request_id=reply_request_id,
                result=result,
                idempotent_replay=pending.idempotent_replay,
            )
            self._task_service.complete_success_in_transaction(
                db=db,
                task_id=current_task.id,
                lease_token=lease_token,
                result_snapshot={"decision": decision.model_dump(mode="json")},
                usage=self._model_usage(),
            )
            return snapshot

    def _resume_existing(
        self,
        db: Session,
        *,
        existing_buyer: Message,
        reply_request_id: str,
        expected_fingerprint: str,
    ) -> ChatTurnSnapshot | _PendingChatTurn:
        if existing_buyer.role is not MessageRole.BUYER:
            raise MessageConflictError("请求幂等键已被其他消息占用")
        if existing_buyer.request_fingerprint != expected_fingerprint:
            raise MessageConflictError("相同请求幂等键不能用于不同请求内容或报价")
        existing_reply = self._message_by_request_id(
            db,
            session_id=existing_buyer.session_id,
            request_id=reply_request_id,
        )
        if existing_reply is not None:
            if existing_reply.role is not MessageRole.AGENT:
                raise MessageConflictError("回复幂等键已被其他消息占用")
            return self._turn_snapshot(
                buyer_snapshot=self._snapshot(existing_buyer),
                agent_message=existing_reply,
                idempotent_replay=True,
            )

        task = db.scalar(
            select(ModelExecutionTask).where(
                ModelExecutionTask.task_type == ModelTaskType.CHAT_DECISION,
                ModelExecutionTask.business_key
                == self._task_business_key(
                    existing_buyer.session_id,
                    existing_buyer.request_id,
                ),
            )
        )
        if task is None:
            raise IncompleteRequestError("该请求尚未形成完整回复，请稍后查询消息记录")
        return _PendingChatTurn(
            session_id=existing_buyer.session_id,
            buyer_snapshot=self._snapshot(existing_buyer),
            task_id=task.id,
            idempotent_replay=True,
        )

    def _persist_result(
        self,
        db: Session,
        *,
        session_id: int,
        buyer_snapshot: MessageSnapshot,
        reply_request_id: str,
        result: AgentTurnResult,
        idempotent_replay: bool,
    ) -> ChatTurnSnapshot:
        agent_message = Message(
            session_id=session_id,
            role=MessageRole.AGENT,
            content=result.reply,
            request_id=reply_request_id,
            agent_outcome=result.outcome.value,
            formal_offer_id=result.formal_offer_id,
        )
        db.add(agent_message)
        db.flush()
        db.refresh(agent_message)
        return ChatTurnSnapshot(
            buyer_message=buyer_snapshot,
            agent_message=self._snapshot(agent_message),
            outcome=result.outcome.value,
            formal_offer_id=result.formal_offer_id,
            idempotent_replay=idempotent_replay,
        )

    def _load_reply(
        self,
        *,
        session_id: int,
        buyer_snapshot: MessageSnapshot,
        idempotent_replay: bool,
    ) -> ChatTurnSnapshot | None:
        with self._session_factory() as db:
            reply = self._message_by_request_id(
                db,
                session_id=session_id,
                request_id=self._reply_request_id(buyer_snapshot.request_id),
            )
            if reply is None:
                return None
            if reply.role is not MessageRole.AGENT:
                raise MessageConflictError("回复幂等键已被其他消息占用")
            return self._turn_snapshot(
                buyer_snapshot=buyer_snapshot,
                agent_message=reply,
                idempotent_replay=idempotent_replay,
            )

    def _pending_from_task(
        self,
        task: ModelExecutionTaskSnapshot,
    ) -> _PendingChatTurn:
        buyer_message_id = task.input_snapshot.get("buyer_message_id")
        if type(buyer_message_id) is not int:
            raise IncompleteRequestError("聊天模型任务缺少买家消息引用")
        with self._session_factory() as db:
            buyer_message = db.get(Message, buyer_message_id)
            if (
                buyer_message is None
                or buyer_message.session_id != task.session_id
                or buyer_message.role is not MessageRole.BUYER
            ):
                raise IncompleteRequestError("聊天模型任务关联的买家消息不存在")
            return _PendingChatTurn(
                session_id=task.session_id,
                buyer_snapshot=self._snapshot(buyer_message),
                task_id=task.id,
                idempotent_replay=True,
            )

    def _build_agent(
        self,
        *,
        session_id: int,
        buyer_id: str,
        session_factory: sessionmaker[Session] | None = None,
        current_turn_offer_id: int | None = None,
    ) -> SellerAgent:
        provider = self._decision_provider
        if provider is None:
            raise RuntimeError("发送消息前必须配置决策模型")
        active_session_factory = session_factory or self._session_factory
        tools = build_seller_tools(
            context=AgentToolContext(
                session_id=session_id,
                buyer_id=buyer_id,
                current_turn_offer_id=current_turn_offer_id,
            ),
            product_service=ProductService(active_session_factory),
            negotiation_service=NegotiationService(active_session_factory),
            approval_service=ApprovalService(active_session_factory),
        )
        return SellerAgent(decision_provider=provider, tools=tools)

    def _stale_reason(
        self,
        db: Session,
        *,
        task: ModelExecutionTaskSnapshot,
        negotiation: NegotiationSession | None,
    ) -> str | None:
        if negotiation is None:
            return "协商会话已不存在"
        product = db.get(Product, negotiation.product_id, with_for_update=True)
        policy = db.scalar(
            select(SellerPolicy)
            .where(SellerPolicy.product_id == negotiation.product_id)
            .with_for_update()
        )
        if product is None or policy is None:
            return "商品或卖家规则已不存在"
        if negotiation.version != task.session_version:
            return "协商会话版本已经变化"
        if policy.version != task.policy_version:
            return "卖家规则版本已经变化"
        if negotiation.current_offer_id != task.offer_id:
            return "当前报价已经变化"
        if task.offer_id is not None:
            offer = db.get(Offer, task.offer_id, with_for_update=True)
            if offer is None or offer.session_id != task.session_id:
                return "任务关联报价已不存在"

        request = self._deserialize_decision_request(task.input_snapshot)
        expected_product = request.product_context.get("product")
        if not isinstance(expected_product, dict):
            return "任务商品快照无效"
        current_product = {
            "id": product.id,
            "title": product.title,
            "description": product.description,
            "listed_price": str(product.listed_price),
            "status": product.status.value,
        }
        if expected_product != current_product:
            return "商品公开信息已经变化"
        expected_negotiation = request.negotiation_context.get("negotiation")
        if not isinstance(expected_negotiation, dict):
            return "任务会话快照无效"
        if (
            expected_negotiation.get("id") != negotiation.id
            or expected_negotiation.get("status") != negotiation.status.value
            or expected_negotiation.get("current_offer_id")
            != negotiation.current_offer_id
            or expected_negotiation.get("version") != negotiation.version
            or expected_negotiation.get("policy_version") != policy.version
        ):
            return "协商上下文已经变化"
        return None

    @staticmethod
    def _serialize_task_input(
        *,
        buyer_message_id: int,
        reply_request_id: str,
        decision_request: DecisionRequest,
    ) -> dict[str, object]:
        return {
            "buyer_message_id": buyer_message_id,
            "reply_request_id": reply_request_id,
            "decision_request": {
                "buyer_message": decision_request.buyer_message,
                "product_context": decision_request.product_context,
                "negotiation_context": decision_request.negotiation_context,
                "conversation_history": [
                    {"role": item.role, "content": item.content}
                    for item in decision_request.conversation_history
                ],
                "current_turn_offer_id": decision_request.current_turn_offer_id,
            },
        }

    @staticmethod
    def _deserialize_decision_request(
        input_snapshot: dict[str, object],
    ) -> DecisionRequest:
        raw_request = input_snapshot.get("decision_request")
        if not isinstance(raw_request, dict):
            raise IncompleteRequestError("模型任务输入快照无效")
        product_context = raw_request.get("product_context")
        negotiation_context = raw_request.get("negotiation_context")
        raw_history = raw_request.get("conversation_history", [])
        if (
            not isinstance(raw_request.get("buyer_message"), str)
            or not isinstance(product_context, dict)
            or not isinstance(negotiation_context, dict)
            or not isinstance(raw_history, list)
        ):
            raise IncompleteRequestError("模型任务输入快照无效")
        history: list[ConversationMessage] = []
        for item in raw_history:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("role"), str)
                or not isinstance(item.get("content"), str)
            ):
                raise IncompleteRequestError("模型任务历史消息快照无效")
            history.append(
                ConversationMessage(role=item["role"], content=item["content"])
            )
        current_offer_id = raw_request.get("current_turn_offer_id")
        if current_offer_id is not None and type(current_offer_id) is not int:
            raise IncompleteRequestError("模型任务报价快照无效")
        return DecisionRequest(
            buyer_message=raw_request["buyer_message"],
            product_context=dict(product_context),
            negotiation_context=dict(negotiation_context),
            conversation_history=tuple(history),
            current_turn_offer_id=current_offer_id,
        )

    @staticmethod
    def _context_current_offer_id(request: DecisionRequest) -> int | None:
        negotiation = request.negotiation_context.get("negotiation")
        if not isinstance(negotiation, dict):
            raise IncompleteRequestError("协商上下文快照无效")
        offer_id = negotiation.get("current_offer_id")
        if offer_id is not None and type(offer_id) is not int:
            raise IncompleteRequestError("协商报价快照无效")
        return offer_id

    @staticmethod
    def _task_reply_request_id(input_snapshot: dict[str, object]) -> str:
        value = input_snapshot.get("reply_request_id")
        if not isinstance(value, str) or not value:
            raise IncompleteRequestError("模型任务回复幂等键无效")
        return value

    @staticmethod
    def _required_lease_token(task: ModelExecutionTaskSnapshot) -> str:
        if task.lease_token is None:  # pragma: no cover - 数据库约束已保证
            raise RuntimeError("已领取模型任务缺少租约令牌")
        return task.lease_token

    @staticmethod
    def _required_buyer_id(negotiation: NegotiationSession | None) -> str:
        if negotiation is None:  # pragma: no cover - 已由 stale 校验处理
            raise NegotiationNotFoundError("协商会话不存在")
        return negotiation.buyer_id

    @staticmethod
    def _transaction_session_factory(db: Session) -> sessionmaker[Session]:
        return sessionmaker(
            bind=db.connection(),
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )

    @staticmethod
    def _has_active_chat_task(db: Session, *, session_id: int) -> bool:
        return (
            db.scalar(
                select(ModelExecutionTask.id)
                .where(
                    ModelExecutionTask.session_id == session_id,
                    ModelExecutionTask.task_type == ModelTaskType.CHAT_DECISION,
                    ModelExecutionTask.status.in_(
                        [
                            ModelTaskStatus.PENDING,
                            ModelTaskStatus.RUNNING,
                            ModelTaskStatus.RETRY_WAIT,
                        ]
                    ),
                )
                .limit(1)
            )
            is not None
        )

    @staticmethod
    def _require_negotiation(
        db: Session,
        *,
        session_id: int,
        buyer_id: str,
        for_update: bool = False,
    ) -> NegotiationSession:
        statement = select(NegotiationSession).where(
            NegotiationSession.id == session_id,
            NegotiationSession.buyer_id == buyer_id,
        )
        if for_update:
            statement = statement.with_for_update()
        negotiation = db.scalar(statement)
        if negotiation is None:
            raise NegotiationNotFoundError("协商会话不存在或当前买家无权访问")
        return negotiation

    def _recent_history(
        self,
        db: Session,
        *,
        session_id: int,
    ) -> tuple[ConversationMessage, ...]:
        items = list(
            db.scalars(
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(Message.id.desc())
                .limit(self._history_limit)
            )
        )
        items.reverse()
        return tuple(
            ConversationMessage(role=item.role.value, content=item.content)
            for item in items
        )

    @staticmethod
    def _message_by_request_id(
        db: Session,
        *,
        session_id: int,
        request_id: str,
    ) -> Message | None:
        return db.scalar(
            select(Message).where(
                Message.session_id == session_id,
                Message.request_id == request_id,
            )
        )

    @staticmethod
    def _task_business_key(session_id: int, request_id: str) -> str:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
        return f"chat:{session_id}:{digest}"

    @staticmethod
    def _reply_request_id(request_id: str) -> str:
        return f"{request_id}:agent"

    @staticmethod
    def _request_fingerprint(
        *,
        content: str,
        offer: BuyerOfferSubmission | None,
    ) -> str:
        offer_payload = None
        if offer is not None:
            offer_payload = {
                "price": format(offer.price, ".2f"),
                "shipping_paid_by": offer.shipping_paid_by.value,
                "shipping_cost": (
                    format(offer.shipping_cost, ".2f")
                    if offer.shipping_cost is not None
                    else None
                ),
                "delivery_method": offer.delivery_method,
            }
        canonical = json.dumps(
            {"content": content, "offer": offer_payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _model_usage(self) -> ModelUsage:
        provider_name = type(self._decision_provider).__name__[:50]
        return ModelUsage(
            provider=provider_name or "unknown",
            model_name="unreported",
        )

    @classmethod
    def _turn_snapshot(
        cls,
        *,
        buyer_snapshot: MessageSnapshot,
        agent_message: Message,
        idempotent_replay: bool,
    ) -> ChatTurnSnapshot:
        return ChatTurnSnapshot(
            buyer_message=buyer_snapshot,
            agent_message=cls._snapshot(agent_message),
            outcome=agent_message.agent_outcome or "IDEMPOTENT_REPLAY",
            formal_offer_id=agent_message.formal_offer_id,
            idempotent_replay=idempotent_replay,
        )

    @staticmethod
    def _snapshot(message: Message) -> MessageSnapshot:
        return MessageSnapshot(
            id=message.id,
            role=message.role,
            content=message.content,
            request_id=message.request_id,
            created_at=message.created_at,
        )
