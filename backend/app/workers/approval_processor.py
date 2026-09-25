import argparse
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.approval_followup import (
    ApprovalFollowupAgentError,
    ApprovalFollowupEvent,
    ApprovalFollowupOutcome,
    ApprovalFollowupProvider,
    ApprovalFollowupRequest,
    LangChainApprovalFollowupProvider,
    SellerApprovalFollowupAgent,
)
from app.agent.model_factory import QwenChatModelFactory
from app.core.config import get_settings
from app.db.models import (
    ApprovalFollowupStatus,
    ApprovalRequest,
    ApprovalStatus,
    Message,
    MessageRole,
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
    """使用数据库行锁领取审批后续任务，并原子写入通知与完成状态。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: ApprovalFollowupProvider,
    ) -> None:
        self._session_factory = session_factory
        self._agent = SellerApprovalFollowupAgent(provider=provider)
        self._negotiation_service = NegotiationService(session_factory)

    def process_batch(self, *, limit: int = 20) -> tuple[ApprovalProcessingResult, ...]:
        """每轮最多尝试一次同一任务，避免失败任务在单轮内立即热重试。"""

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
        """领取并处理一个任务；事务中断时数据库会自动释放行锁并保留任务。"""

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
            return self._process_locked(db, approval=approval)

    def _process_locked(
        self,
        db: Session,
        *,
        approval: ApprovalRequest,
    ) -> ApprovalProcessingResult:
        request_id = approval.followup_request_id
        if not request_id or len(request_id) > 64:
            return self._mark_failed(db, approval=approval)

        existing_message = db.scalar(
            select(Message).where(
                Message.session_id == approval.session_id,
                Message.request_id == request_id,
            )
        )
        if existing_message is not None:
            stored_outcome = self._stored_outcome(existing_message.agent_outcome)
            if (
                existing_message.role is not MessageRole.AGENT
                or stored_outcome is None
            ):
                return self._mark_failed(db, approval=approval)
            approval.followup_status = ApprovalFollowupStatus.SENT
            db.flush()
            return ApprovalProcessingResult(
                approval_id=approval.id,
                status=ApprovalFollowupStatus.SENT,
                message_id=existing_message.id,
                outcome=stored_outcome,
            )

        try:
            context = self._load_context(db, approval=approval)
            event = self._resolve_event(context)
            result = self._agent.handle_followup(
                request=self._agent_request(context, event=event),
                offer_result=self._offer_result(context.offer),
            )
        except (ApprovalFollowupAgentError, ApprovalFollowupProcessingError):
            return self._mark_failed(db, approval=approval)

        self._apply_successful_event(context, event=event)
        message = Message(
            session_id=approval.session_id,
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
        approval.followup_status = ApprovalFollowupStatus.SENT
        db.flush()
        db.refresh(message)
        return ApprovalProcessingResult(
            approval_id=approval.id,
            status=ApprovalFollowupStatus.SENT,
            message_id=message.id,
            outcome=result.outcome,
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
            return 1 if any(item.status is ApprovalFollowupStatus.FAILED for item in results) else 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
