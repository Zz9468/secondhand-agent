import argparse
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.approval_followup import (
    ApprovalFollowupAgentError,
    ApprovalFollowupDraft,
    ApprovalFollowupEvent,
    ApprovalFollowupOutcome,
    ApprovalFollowupProvider,
    ApprovalFollowupRequest,
    ApprovalFollowupResult,
    LangChainApprovalFollowupProvider,
    SellerApprovalFollowupAgent,
)
from app.agent.model_factory import QwenChatModelFactory
from app.agent.model_observation import ObservedProviderResult, ProviderUsage
from app.core.config import get_settings
from app.db.models import (
    ApprovalFollowupStatus,
    ApprovalRequest,
    ApprovalStatus,
    Message,
    MessageRole,
    ModelTaskStatus,
    ModelTaskType,
    NegotiationSession,
    NegotiationStatus,
    Offer,
    OfferProposer,
    OfferStatus,
    Product,
    ProductStatus,
    SellerPolicy,
)
from app.db.session import get_session_factory
from app.observability.context import observation_scope
from app.observability.service import ObservabilityEventInput, ObservabilityService
from app.services.errors import ModelExecutionTaskLeaseError
from app.services.model_retry_policy import ModelRetryPolicy
from app.services.model_task_service import (
    ModelExecutionTaskSnapshot,
    ModelTaskService,
    ModelUsage,
)
from app.services.model_usage_service import ModelUsageService
from app.services.negotiation_service import NegotiationService

logger = logging.getLogger(__name__)


class ApprovalFollowupProcessingError(RuntimeError):
    """后续任务缺少必要业务事实或状态不一致。"""


@dataclass(frozen=True, slots=True)
class ApprovalProcessingResult:
    approval_id: int
    status: ApprovalFollowupStatus
    message_id: int | None
    outcome: ApprovalFollowupOutcome | None


@dataclass(frozen=True, slots=True)
class _FollowupContext:
    approval: ApprovalRequest
    negotiation: NegotiationSession
    product: Product
    offer: Offer
    policy: SellerPolicy | None


class ApprovalProcessor:
    """以短事务领取和完成审批通知，模型调用期间不持有业务行锁。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: ApprovalFollowupProvider,
        retry_policy: ModelRetryPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._agent = SellerApprovalFollowupAgent(provider=provider)
        self._provider = provider
        self._negotiation_service = NegotiationService(session_factory)
        self._task_service = ModelTaskService(session_factory)
        self._settings = get_settings()
        self._usage_service = ModelUsageService(self._settings)
        self._observer = ObservabilityService(session_factory)
        self._retry_policy = retry_policy or ModelRetryPolicy.from_settings(
            self._settings
        )

    def process_batch(self, *, limit: int = 20) -> tuple[ApprovalProcessingResult, ...]:
        """每轮最多尝试一次同一审批，避免失败任务在单轮内热重试。"""

        if not 1 <= limit <= 200:
            raise ValueError("单轮审批后续任务数量必须在 1 到 200 之间")
        attempted_ids: set[int] = set()
        results: list[ApprovalProcessingResult] = []
        while len(results) < limit:
            result = self.process_next(excluded_approval_ids=attempted_ids)
            if result is None:
                break
            attempted_ids.add(result.approval_id)
            results.append(result)
        return tuple(results)

    def process_next(
        self,
        *,
        excluded_approval_ids: set[int] | None = None,
    ) -> ApprovalProcessingResult | None:
        leased = self._claim_next(excluded_approval_ids=excluded_approval_ids)
        if leased is None or isinstance(leased, ApprovalProcessingResult):
            return leased

        with observation_scope(
            correlation_id=leased.correlation_id,
            session_id=leased.session_id,
            model_task_id=leased.id,
        ):
            return self._process_leased(leased)

    def _process_leased(
        self,
        leased: ModelExecutionTaskSnapshot,
    ) -> ApprovalProcessingResult:

        lease_token = leased.lease_token
        if lease_token is None:  # pragma: no cover - 数据库约束已保证
            raise RuntimeError("已领取审批通知任务缺少租约令牌")
        started = time.perf_counter()
        self._observer.record(
            ObservabilityEventInput(
                event_type="MODEL_TASK_ATTEMPT_STARTED",
                action="APPROVAL_FOLLOWUP",
                outcome="STARTED",
                approval_id=leased.approval_id,
                offer_id=leased.offer_id,
                attempt_count=leased.attempt_count,
                attributes={"task_type": leased.task_type.value},
            )
        )
        try:
            request, _ = self._task_input(leased.input_snapshot)
            draft, usage = self._draft_with_usage(request)
        except (ApprovalFollowupAgentError, KeyError, TypeError, ValueError) as exc:
            plan = self._retry_policy.plan_failure(
                task_id=leased.id,
                attempt_count=leased.attempt_count,
                max_attempts=leased.max_attempts,
                error=exc,
            )
            self._observer.record(
                ObservabilityEventInput(
                    event_type="MODEL_CALL_COMPLETED",
                    action="APPROVAL_FOLLOWUP",
                    outcome="ERROR",
                    approval_id=leased.approval_id,
                    offer_id=leased.offer_id,
                    attempt_count=leased.attempt_count,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    error_category=plan.category.value,
                    attributes={
                        "model_call_purpose": "APPROVAL_FOLLOWUP",
                        "retry_scheduled": plan.should_retry,
                    },
                )
            )
            try:
                return self._handle_failed_task(
                    task=leased,
                    lease_token=lease_token,
                    error=exc,
                )
            except ModelExecutionTaskLeaseError:
                return self._lease_lost_result(leased)
        self._observer.record(
            ObservabilityEventInput(
                event_type="MODEL_CALL_COMPLETED",
                action="APPROVAL_FOLLOWUP",
                outcome="SUCCESS",
                approval_id=leased.approval_id,
                offer_id=leased.offer_id,
                attempt_count=leased.attempt_count,
                duration_ms=round((time.perf_counter() - started) * 1000),
                usage=usage,
                attributes={"model_call_purpose": "APPROVAL_FOLLOWUP"},
            )
        )
        try:
            result = self._finalize_followup(
                task=leased,
                lease_token=lease_token,
                draft=draft,
                usage=usage,
            )
            self._observer.record(
                ObservabilityEventInput(
                    event_type="APPROVAL_FOLLOWUP_COMPLETED",
                    action="APPLY_APPROVAL_FOLLOWUP",
                    outcome="SUCCESS",
                    approval_id=result.approval_id,
                    offer_id=leased.offer_id,
                    attributes={
                        "approval_status": result.status.value,
                        "agent_outcome": (
                            result.outcome.value if result.outcome else None
                        ),
                    },
                )
            )
            return result
        except ModelExecutionTaskLeaseError:
            return self._lease_lost_result(leased)

    def _claim_next(
        self,
        *,
        excluded_approval_ids: set[int] | None,
    ) -> ModelExecutionTaskSnapshot | ApprovalProcessingResult | None:
        with self._session_factory() as db, db.begin():
            statement = (
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.status.in_(
                        [ApprovalStatus.APPROVED, ApprovalStatus.REJECTED]
                    ),
                    ApprovalRequest.followup_status.in_(
                        [
                            ApprovalFollowupStatus.PENDING,
                            ApprovalFollowupStatus.FAILED,
                        ]
                    ),
                )
                .order_by(
                    case(
                        (
                            ApprovalRequest.followup_status
                            == ApprovalFollowupStatus.PENDING,
                            0,
                        ),
                        else_=1,
                    ),
                    ApprovalRequest.id,
                )
                .with_for_update(skip_locked=True)
            )
            if excluded_approval_ids:
                statement = statement.where(
                    ApprovalRequest.id.not_in(excluded_approval_ids)
                )
            approval = db.scalar(statement.limit(1))
            if approval is None:
                return None

            request_id = approval.followup_request_id
            if not request_id or len(request_id) > 64:
                return self._mark_manual_required(db, approval=approval)
            existing = self._existing_message(
                db,
                session_id=approval.session_id,
                request_id=request_id,
            )
            if existing is not None:
                stored_outcome = self._stored_outcome(existing.agent_outcome)
                if existing.role is not MessageRole.AGENT or stored_outcome is None:
                    return self._mark_manual_required(db, approval=approval)
                approval.followup_status = ApprovalFollowupStatus.SENT
                db.flush()
                return ApprovalProcessingResult(
                    approval_id=approval.id,
                    status=ApprovalFollowupStatus.SENT,
                    message_id=existing.id,
                    outcome=stored_outcome,
                )

            try:
                context = self._load_context(db, approval=approval)
                event = self._resolve_event(context)
                request = self._agent_request(context, event=event)
                offer_result = self._offer_result(context.offer)
                creation = self._task_service.create_task_in_transaction(
                    db=db,
                    task_type=ModelTaskType.APPROVAL_FOLLOWUP,
                    business_key=f"approval:{approval.id}",
                    session_id=approval.session_id,
                    offer_id=approval.offer_id,
                    approval_id=approval.id,
                    input_snapshot=self._serialize_task_input(
                        request=request,
                        offer_result=offer_result,
                    ),
                    max_attempts=self._retry_policy.max_attempts,
                )
            except ApprovalFollowupProcessingError:
                return self._mark_manual_required(db, approval=approval)

            leased = self._task_service.lease_task_in_transaction(
                db=db,
                task_id=creation.task.id,
                worker_id=f"approval:{approval.id}",
                lease_seconds=self._retry_policy.lease_seconds,
            )
            if leased is not None:
                if leased.status is ModelTaskStatus.FAILED:
                    return self._mark_manual_required(db, approval=approval)
                return leased
            if creation.task.status is ModelTaskStatus.RETRY_WAIT:
                return self._mark_failed(db, approval=approval)
            if creation.task.status in {
                ModelTaskStatus.FAILED,
                ModelTaskStatus.CANCELLED,
                ModelTaskStatus.SUCCEEDED,
            }:
                return self._mark_manual_required(db, approval=approval)
            return None

    def _finalize_followup(
        self,
        *,
        task: ModelExecutionTaskSnapshot,
        lease_token: str,
        draft: ApprovalFollowupDraft,
        usage: ModelUsage,
    ) -> ApprovalProcessingResult:
        with self._session_factory() as db, db.begin():
            approval = db.get(
                ApprovalRequest,
                task.approval_id,
                with_for_update=True,
            )
            # 与领取事务保持 approval → task 的加锁顺序，避免并发领取和完成互锁。
            current_task = self._task_service.require_active_lease_in_transaction(
                db=db,
                task_id=task.id,
                lease_token=lease_token,
            )
            if approval is None:
                self._task_service.mark_stale_in_transaction(
                    db=db,
                    task_id=current_task.id,
                    lease_token=lease_token,
                    error_message="审批记录已不存在",
                    usage=usage,
                )
                return ApprovalProcessingResult(
                    approval_id=task.approval_id or 0,
                    status=ApprovalFollowupStatus.FAILED,
                    message_id=None,
                    outcome=None,
                )

            request_id = approval.followup_request_id
            if not request_id:
                self._task_service.mark_stale_in_transaction(
                    db=db,
                    task_id=current_task.id,
                    lease_token=lease_token,
                    error_message="审批通知幂等键已不存在",
                    usage=usage,
                )
                return self._mark_manual_required(db, approval=approval)
            existing = self._existing_message(
                db,
                session_id=approval.session_id,
                request_id=request_id,
            )
            if existing is not None:
                stored_outcome = self._stored_outcome(existing.agent_outcome)
                if existing.role is not MessageRole.AGENT or stored_outcome is None:
                    self._task_service.mark_stale_in_transaction(
                        db=db,
                        task_id=current_task.id,
                        lease_token=lease_token,
                        error_message="审批通知幂等键已被其他消息占用",
                        usage=usage,
                    )
                    return self._mark_manual_required(db, approval=approval)
                approval.followup_status = ApprovalFollowupStatus.SENT
                self._task_service.mark_stale_in_transaction(
                    db=db,
                    task_id=current_task.id,
                    lease_token=lease_token,
                    error_message="审批通知已经由其他执行者写入",
                    usage=usage,
                )
                return ApprovalProcessingResult(
                    approval_id=approval.id,
                    status=ApprovalFollowupStatus.SENT,
                    message_id=existing.id,
                    outcome=stored_outcome,
                )

            try:
                context = self._load_context(db, approval=approval)
                current_event = self._resolve_event(context)
            except ApprovalFollowupProcessingError as exc:
                self._task_service.mark_stale_in_transaction(
                    db=db,
                    task_id=current_task.id,
                    lease_token=lease_token,
                    error_message=str(exc),
                    usage=usage,
                )
                return self._mark_manual_required(db, approval=approval)

            original_request, original_offer_result = self._task_input(
                current_task.input_snapshot
            )
            current_request = self._agent_request(context, event=current_event)
            current_offer_result = self._offer_result(context.offer)
            stale_reason = self._stale_reason(
                task=current_task,
                context=context,
                original_request=original_request,
                current_request=current_request,
            )

            if stale_reason is None:
                try:
                    result = self._agent.apply_draft(
                        request=original_request,
                        offer_result=original_offer_result,
                        draft=draft,
                    )
                except ApprovalFollowupAgentError as exc:
                    status = self._apply_failure_in_transaction(
                        db=db,
                        approval=approval,
                        task=current_task,
                        lease_token=lease_token,
                        error=ValueError(str(exc)),
                    )
                    return ApprovalProcessingResult(
                        approval_id=approval.id,
                        status=status,
                        message_id=None,
                        outcome=None,
                    )
                self._apply_successful_event(context, event=current_event)
                message = self._persist_message(
                    db,
                    context=context,
                    request_id=request_id,
                    event=current_event,
                    result=result,
                )
                self._task_service.complete_success_in_transaction(
                    db=db,
                    task_id=current_task.id,
                    lease_token=lease_token,
                    result_snapshot={"draft": draft.model_dump(mode="json")},
                    usage=usage,
                )
                return ApprovalProcessingResult(
                    approval_id=approval.id,
                    status=ApprovalFollowupStatus.SENT,
                    message_id=message.id,
                    outcome=result.outcome,
                )

            result = self._safe_stale_result(
                context=context,
                event=current_event,
                request=current_request,
                offer_result=current_offer_result,
            )
            message = self._persist_message(
                db,
                context=context,
                request_id=request_id,
                event=(
                    current_event
                    if current_event is ApprovalFollowupEvent.INVALIDATED
                    and context.negotiation.current_offer_id == context.offer.id
                    else ApprovalFollowupEvent.INVALIDATED
                ),
                result=result,
            )
            self._task_service.mark_stale_in_transaction(
                db=db,
                task_id=current_task.id,
                lease_token=lease_token,
                error_message=stale_reason,
                usage=usage,
            )
            return ApprovalProcessingResult(
                approval_id=approval.id,
                status=ApprovalFollowupStatus.SENT,
                message_id=message.id,
                outcome=result.outcome,
            )

    def _safe_stale_result(
        self,
        *,
        context: _FollowupContext,
        event: ApprovalFollowupEvent,
        request: ApprovalFollowupRequest,
        offer_result: dict[str, object],
    ) -> ApprovalFollowupResult:
        if (
            event is ApprovalFollowupEvent.INVALIDATED
            and context.negotiation.current_offer_id == context.offer.id
        ):
            self._apply_successful_event(context, event=event)
            return self._agent.render_trusted_event(
                request=request,
                offer_result=offer_result,
            )
        return ApprovalFollowupResult(
            reply="审批期间协商条件已经变化，原审批结果未应用，请以当前会话状态为准。",
            outcome=ApprovalFollowupOutcome.APPROVAL_INVALIDATED,
        )

    def _handle_failed_task(
        self,
        *,
        task: ModelExecutionTaskSnapshot,
        lease_token: str,
        error: BaseException,
    ) -> ApprovalProcessingResult:
        with self._session_factory() as db, db.begin():
            approval = db.get(
                ApprovalRequest,
                task.approval_id,
                with_for_update=True,
            )
            status = self._apply_failure_in_transaction(
                db=db,
                approval=approval,
                task=task,
                lease_token=lease_token,
                error=error,
            )
            return ApprovalProcessingResult(
                approval_id=task.approval_id or 0,
                status=status,
                message_id=None,
                outcome=None,
            )

    def _apply_failure_in_transaction(
        self,
        *,
        db: Session,
        approval: ApprovalRequest | None,
        task: ModelExecutionTaskSnapshot,
        lease_token: str,
        error: BaseException,
    ) -> ApprovalFollowupStatus:
        plan = self._retry_policy.plan_failure(
            task_id=task.id,
            attempt_count=task.attempt_count,
            max_attempts=task.max_attempts,
            error=error,
        )
        if plan.should_retry and plan.next_retry_at is not None:
            self._task_service.defer_retry_in_transaction(
                db=db,
                task_id=task.id,
                lease_token=lease_token,
                error_category=plan.category,
                error_message=plan.safe_message,
                next_retry_at=plan.next_retry_at,
            )
            status = ApprovalFollowupStatus.FAILED
        else:
            self._task_service.fail_task_in_transaction(
                db=db,
                task_id=task.id,
                lease_token=lease_token,
                error_category=plan.category,
                error_message=plan.safe_message,
            )
            status = ApprovalFollowupStatus.MANUAL_REQUIRED
        if approval is not None:
            approval.followup_status = status
            db.flush()
        return status

    def _lease_lost_result(
        self,
        task: ModelExecutionTaskSnapshot,
    ) -> ApprovalProcessingResult:
        """旧 Worker 迟到时只读取现状，绝不覆盖新租约或新业务事实。"""

        with self._session_factory() as db:
            approval = db.get(ApprovalRequest, task.approval_id)
            if approval is None:
                return ApprovalProcessingResult(
                    approval_id=task.approval_id or 0,
                    status=ApprovalFollowupStatus.FAILED,
                    message_id=None,
                    outcome=None,
                )
            request_id = approval.followup_request_id
            message = (
                self._existing_message(
                    db,
                    session_id=approval.session_id,
                    request_id=request_id,
                )
                if request_id
                else None
            )
            outcome = (
                self._stored_outcome(message.agent_outcome)
                if message is not None
                else None
            )
            return ApprovalProcessingResult(
                approval_id=approval.id,
                status=approval.followup_status,
                message_id=message.id if message is not None else None,
                outcome=outcome,
            )

    @staticmethod
    def _load_context(
        db: Session,
        *,
        approval: ApprovalRequest,
    ) -> _FollowupContext:
        negotiation = db.get(
            NegotiationSession,
            approval.session_id,
            with_for_update=True,
        )
        offer = db.get(Offer, approval.offer_id, with_for_update=True)
        if negotiation is None or offer is None or offer.session_id != approval.session_id:
            raise ApprovalFollowupProcessingError("审批关联的会话或报价不存在")
        product = db.get(Product, negotiation.product_id, with_for_update=True)
        if product is None:
            raise ApprovalFollowupProcessingError("审批关联的商品不存在")
        policy = db.scalar(
            select(SellerPolicy)
            .where(SellerPolicy.product_id == product.id)
            .with_for_update()
        )
        return _FollowupContext(
            approval=approval,
            negotiation=negotiation,
            product=product,
            offer=offer,
            policy=policy,
        )

    def _resolve_event(self, context: _FollowupContext) -> ApprovalFollowupEvent:
        if context.offer.proposer is not OfferProposer.BUYER:
            raise ApprovalFollowupProcessingError("审批关联的不是买家报价")
        if context.approval.status is ApprovalStatus.REJECTED:
            if context.offer.status is not OfferStatus.REJECTED:
                raise ApprovalFollowupProcessingError("被拒绝报价的状态不一致")
            return ApprovalFollowupEvent.REJECTED

        if context.approval.status is not ApprovalStatus.APPROVED:
            raise ApprovalFollowupProcessingError("审批结果不支持生成后续通知")
        if (
            context.negotiation.status is not NegotiationStatus.WAITING_APPROVAL
            or context.negotiation.current_offer_id != context.offer.id
            or context.product.status is not ProductStatus.AVAILABLE
            or context.offer.status is not OfferStatus.PROPOSED
            or context.policy is None
            or context.policy.version != context.approval.policy_version
            or (
                context.offer.expires_at is not None
                and context.offer.expires_at <= datetime.now()
            )
        ):
            return ApprovalFollowupEvent.INVALIDATED
        try:
            authorization = self._negotiation_service.authorize_stored_offer(
                offer=context.offer,
                policy=context.policy,
            )
        except Exception as exc:
            raise ApprovalFollowupProcessingError("审批报价无法重新授权") from exc
        if not authorization.can_request_approval:
            return ApprovalFollowupEvent.INVALIDATED
        return ApprovalFollowupEvent.APPROVED

    @staticmethod
    def _apply_successful_event(
        context: _FollowupContext,
        *,
        event: ApprovalFollowupEvent,
    ) -> None:
        if event is ApprovalFollowupEvent.APPROVED:
            context.offer.status = OfferStatus.ACCEPTED
        elif (
            event is ApprovalFollowupEvent.INVALIDATED
            and context.offer.status is OfferStatus.PROPOSED
        ):
            context.offer.status = OfferStatus.WITHDRAWN
        if context.negotiation.status is NegotiationStatus.WAITING_APPROVAL:
            context.negotiation.status = NegotiationStatus.ACTIVE
            context.negotiation.version += 1

    @staticmethod
    def _agent_request(
        context: _FollowupContext,
        *,
        event: ApprovalFollowupEvent,
    ) -> ApprovalFollowupRequest:
        return ApprovalFollowupRequest(
            approval_id=context.approval.id,
            event=event,
            product_title=context.product.title,
            offer_id=context.offer.id,
            price=format(context.offer.price, ".2f"),
            shipping_paid_by=context.offer.shipping_paid_by.value,
            shipping_cost=(
                format(context.offer.shipping_cost, ".2f")
                if context.offer.shipping_cost is not None
                else None
            ),
            seller_borne_discount=format(
                context.offer.seller_borne_discount,
                ".2f",
            ),
            additional_terms=dict(context.offer.terms),
            seller_comment=context.approval.seller_comment,
        )

    @staticmethod
    def _offer_result(offer: Offer) -> dict[str, object]:
        return {
            "ok": True,
            "offer": {
                "id": offer.id,
                "proposer": offer.proposer.value,
                "price": format(offer.price, ".2f"),
                "shipping_paid_by": offer.shipping_paid_by.value,
                "shipping_cost": (
                    format(offer.shipping_cost, ".2f")
                    if offer.shipping_cost is not None
                    else None
                ),
                "seller_borne_discount": format(
                    offer.seller_borne_discount,
                    ".2f",
                ),
                "additional_terms": dict(offer.terms),
                "status": offer.status.value,
            },
        }

    @staticmethod
    def _serialize_task_input(
        *,
        request: ApprovalFollowupRequest,
        offer_result: dict[str, object],
    ) -> dict[str, object]:
        return {
            "request": {
                "approval_id": request.approval_id,
                "event": request.event.value,
                "product_title": request.product_title,
                "offer_id": request.offer_id,
                "price": request.price,
                "shipping_paid_by": request.shipping_paid_by,
                "shipping_cost": request.shipping_cost,
                "seller_borne_discount": request.seller_borne_discount,
                "additional_terms": request.additional_terms,
                "seller_comment": request.seller_comment,
            },
            "offer_result": offer_result,
        }

    @staticmethod
    def _task_input(
        input_snapshot: dict[str, object],
    ) -> tuple[ApprovalFollowupRequest, dict[str, object]]:
        raw = input_snapshot.get("request")
        offer_result = input_snapshot.get("offer_result")
        if not isinstance(raw, dict) or not isinstance(offer_result, dict):
            raise ApprovalFollowupProcessingError("审批模型任务输入快照无效")
        try:
            request = ApprovalFollowupRequest(
                approval_id=int(raw["approval_id"]),
                event=ApprovalFollowupEvent(str(raw["event"])),
                product_title=str(raw["product_title"]),
                offer_id=int(raw["offer_id"]),
                price=str(raw["price"]),
                shipping_paid_by=str(raw["shipping_paid_by"]),
                shipping_cost=(
                    str(raw["shipping_cost"])
                    if raw.get("shipping_cost") is not None
                    else None
                ),
                seller_borne_discount=str(raw["seller_borne_discount"]),
                additional_terms=dict(raw.get("additional_terms") or {}),
                seller_comment=(
                    str(raw["seller_comment"])
                    if raw.get("seller_comment") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ApprovalFollowupProcessingError(
                "审批模型任务输入快照无效"
            ) from exc
        return request, dict(offer_result)

    @staticmethod
    def _stale_reason(
        *,
        task: ModelExecutionTaskSnapshot,
        context: _FollowupContext,
        original_request: ApprovalFollowupRequest,
        current_request: ApprovalFollowupRequest,
    ) -> str | None:
        if task.session_version != context.negotiation.version:
            return "审批处理期间会话版本已经变化"
        if context.policy is None or task.policy_version != context.policy.version:
            return "审批处理期间卖家规则已经变化"
        if task.offer_id != context.offer.id or task.approval_id != context.approval.id:
            return "审批任务业务引用已经变化"
        if original_request != current_request:
            return "审批处理期间可信输入已经变化"
        return None

    @staticmethod
    def _existing_message(
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
    def _persist_message(
        db: Session,
        *,
        context: _FollowupContext,
        request_id: str,
        event: ApprovalFollowupEvent,
        result: ApprovalFollowupResult,
    ) -> Message:
        message = Message(
            session_id=context.approval.session_id,
            role=MessageRole.AGENT,
            content=result.reply,
            request_id=request_id,
            agent_outcome=result.outcome.value,
            formal_offer_id=(
                context.offer.id
                if event is ApprovalFollowupEvent.APPROVED
                else None
            ),
        )
        db.add(message)
        context.approval.followup_status = ApprovalFollowupStatus.SENT
        db.flush()
        db.refresh(message)
        return message

    def _draft_with_usage(
        self,
        request: ApprovalFollowupRequest,
    ) -> tuple[ApprovalFollowupDraft, ModelUsage]:
        try:
            observed_method = getattr(self._provider, "draft_with_usage", None)
            if callable(observed_method):
                observed = observed_method(request)
            else:
                observed = ObservedProviderResult(
                    value=self._provider.draft(request),
                    usage=ProviderUsage(
                        provider=type(self._provider).__name__[:50] or "unknown",
                        model_name="unreported",
                    ),
                )
        except ApprovalFollowupAgentError:
            raise
        except Exception as exc:
            raise ApprovalFollowupAgentError("审批结果通知生成失败") from exc
        return observed.value, self._usage_service.build(observed.usage)

    @staticmethod
    def _mark_failed(
        db: Session,
        *,
        approval: ApprovalRequest,
    ) -> ApprovalProcessingResult:
        approval.followup_status = ApprovalFollowupStatus.FAILED
        db.flush()
        return ApprovalProcessingResult(
            approval_id=approval.id,
            status=ApprovalFollowupStatus.FAILED,
            message_id=None,
            outcome=None,
        )

    @staticmethod
    def _mark_manual_required(
        db: Session,
        *,
        approval: ApprovalRequest,
    ) -> ApprovalProcessingResult:
        approval.followup_status = ApprovalFollowupStatus.MANUAL_REQUIRED
        db.flush()
        return ApprovalProcessingResult(
            approval_id=approval.id,
            status=ApprovalFollowupStatus.MANUAL_REQUIRED,
            message_id=None,
            outcome=None,
        )

    @staticmethod
    def _stored_outcome(value: str | None) -> ApprovalFollowupOutcome | None:
        if value is None:
            return None
        try:
            return ApprovalFollowupOutcome(value)
        except ValueError:
            return None


def _build_processor() -> ApprovalProcessor:
    model = QwenChatModelFactory().create(get_settings())
    provider = LangChainApprovalFollowupProvider(model)
    return ApprovalProcessor(get_session_factory(), provider)


def main() -> int:
    parser = argparse.ArgumentParser(description="处理卖家审批后的买家通知任务")
    parser.add_argument("--once", action="store_true", help="只处理一批任务后退出")
    parser.add_argument("--batch-size", type=int, default=20, help="每轮最多处理任务数")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="轮询间隔秒数")
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds 必须大于 0")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    processor = _build_processor()
    while True:
        results = processor.process_batch(limit=args.batch_size)
        for result in results:
            logger.info(
                "approval_followup approval_id=%s status=%s outcome=%s",
                result.approval_id,
                result.status.value,
                result.outcome.value if result.outcome else None,
            )
        if args.once:
            return (
                1
                if any(
                    item.status is ApprovalFollowupStatus.FAILED for item in results
                )
                else 0
            )
        time.sleep(args.poll_seconds)


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
