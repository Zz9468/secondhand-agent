import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    ApprovalFollowupStatus,
    ApprovalRequest,
    ApprovalStatus,
    NegotiationSession,
    NegotiationStatus,
    Offer,
    OfferProposer,
    OfferStatus,
    Product,
    ProductStatus,
    SellerPolicy,
)
from app.services.errors import (
    ApprovalConflictError,
    ApprovalNotAuthorizedError,
    ApprovalNotExpiredError,
    ApprovalNotFoundError,
    InvalidNegotiationStateError,
    NegotiationNotFoundError,
    PricingPolicyNotFoundError,
    ProductUnavailableError,
)
from app.services.negotiation_service import NegotiationService, OfferSnapshot
from app.services.pricing_service import PricingService

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


@dataclass(frozen=True, slots=True)
class ApprovalSnapshot:
    id: int
    session_id: int
    offer_id: int
    policy_version: int
    status: ApprovalStatus
    reason: str
    seller_comment: str | None
    expires_at: datetime
    reviewed_at: datetime | None
    followup_status: ApprovalFollowupStatus | None
    followup_request_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SellerApprovalSnapshot:
    """卖家审批页面使用的审批、商品、会话和报价联合快照。"""

    id: int
    session_id: int
    product_id: int
    product_title: str
    offer_id: int
    policy_version: int
    status: ApprovalStatus
    reason: str
    seller_comment: str | None
    expires_at: datetime
    reviewed_at: datetime | None
    followup_status: ApprovalFollowupStatus | None
    followup_request_id: str | None
    created_at: datetime
    updated_at: datetime
    session_status: NegotiationStatus
    current_offer_id: int | None
    offer: OfferSnapshot


class ApprovalService:
    """以数据库事实校验并持久化人工审批状态。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        pricing_service: PricingService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._negotiation_service = NegotiationService(
            session_factory,
            pricing_service,
        )

    def create_request(
        self,
        *,
        session_id: int,
        buyer_id: str,
        offer_id: int,
        expected_policy_version: int,
        reason: str,
        expires_at: datetime,
    ) -> ApprovalSnapshot:
        """为当前审批区报价创建唯一待审批记录。"""

        stored_reason = self._validated_reason(reason)
        stored_expiration = self._as_database_datetime(expires_at)
        if self._is_due(stored_expiration):
            raise ApprovalConflictError("审批失效时间必须晚于当前时间")

        with self._session_factory() as db, db.begin():
            negotiation = self._get_buyer_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
                for_update=True,
            )
            pending = self._get_pending(db, session_id=session_id, for_update=True)
            if pending is not None:
                if self._is_due(pending.expires_at):
                    raise ApprovalConflictError(
                        "待审批请求已经到期，请先执行过期处理并提交新报价"
                    )
                if (
                    negotiation.status is NegotiationStatus.WAITING_APPROVAL
                    and pending.offer_id == offer_id
                    and pending.policy_version == expected_policy_version
                ):
                    return self._snapshot(pending)
                raise ApprovalConflictError("当前会话已经存在待审批请求")

            if negotiation.status is not NegotiationStatus.ACTIVE:
                raise InvalidNegotiationStateError("当前会话状态不能创建审批")
            product = db.get(Product, negotiation.product_id, with_for_update=True)
            if product is None or product.status is not ProductStatus.AVAILABLE:
                raise ProductUnavailableError("商品当前不可协商")
            if negotiation.current_offer_id != offer_id:
                raise ApprovalConflictError("只能审批当前有效报价")

            offer = db.scalar(
                select(Offer)
                .where(
                    Offer.id == offer_id,
                    Offer.session_id == negotiation.id,
                )
                .with_for_update()
            )
            if offer is None:
                raise ApprovalNotFoundError("待审批报价不存在")
            self._validate_offer(offer, approval_expires_at=stored_expiration)

            policy = self._get_policy(
                db,
                product_id=negotiation.product_id,
                for_update=True,
            )
            if policy.version != expected_policy_version:
                raise ApprovalConflictError("卖家规则版本已经变化，请重新评估报价")
            authorization = self._negotiation_service.authorize_stored_offer(
                offer=offer,
                policy=policy,
            )
            if not authorization.can_request_approval:
                raise ApprovalNotAuthorizedError("当前报价不在人工审批区")

            approval = ApprovalRequest(
                session_id=negotiation.id,
                offer_id=offer.id,
                policy_version=policy.version,
                status=ApprovalStatus.PENDING,
                reason=stored_reason,
                expires_at=stored_expiration,
            )
            db.add(approval)
            negotiation.status = NegotiationStatus.WAITING_APPROVAL
            negotiation.version += 1
            try:
                db.flush()
            except IntegrityError as exc:
                raise ApprovalConflictError(
                    "当前会话或报价已经存在审批请求"
                ) from exc
            return self._snapshot(approval)

    def get_request(
        self,
        *,
        session_id: int,
        buyer_id: str,
        approval_id: int,
    ) -> ApprovalSnapshot:
        with self._session_factory() as db:
            self._get_buyer_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
            )
            approval = self._get_approval(
                db,
                approval_id=approval_id,
                session_id=session_id,
            )
            return self._snapshot(approval)

    def list_for_session(
        self,
        *,
        session_id: int,
        buyer_id: str,
    ) -> tuple[ApprovalSnapshot, ...]:
        with self._session_factory() as db:
            self._get_buyer_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
            )
            approvals = db.scalars(
                select(ApprovalRequest)
                .where(ApprovalRequest.session_id == session_id)
                .order_by(ApprovalRequest.id)
            )
            return tuple(self._snapshot(approval) for approval in approvals)

    def list_for_seller(
        self,
        *,
        seller_id: str,
        approval_status: ApprovalStatus | None = None,
        limit: int = 200,
    ) -> tuple[SellerApprovalSnapshot, ...]:
        """列出当前卖家拥有商品的审批，并顺带清理已经到期的待审批项。"""

        if not 1 <= limit <= 500:
            raise ValueError("审批列表数量必须在 1 到 500 之间")
        with self._session_factory() as db, db.begin():
            self._expire_due_owned(db, seller_id=seller_id)
            statement = (
                select(ApprovalRequest, NegotiationSession, Product, Offer)
                .join(
                    NegotiationSession,
                    NegotiationSession.id == ApprovalRequest.session_id,
                )
                .join(Product, Product.id == NegotiationSession.product_id)
                .join(Offer, Offer.id == ApprovalRequest.offer_id)
                .where(Product.seller_id == seller_id)
                .order_by(ApprovalRequest.id.desc())
                .limit(limit)
            )
            if approval_status is not None:
                statement = statement.where(
                    ApprovalRequest.status == approval_status
                )
            rows = db.execute(statement)
            return tuple(
                self._seller_snapshot(
                    approval=approval,
                    negotiation=negotiation,
                    product=product,
                    offer=offer,
                )
                for approval, negotiation, product, offer in rows
            )

    def get_for_seller(
        self,
        *,
        seller_id: str,
        approval_id: int,
    ) -> SellerApprovalSnapshot:
        """读取卖家拥有商品的单条审批；其他卖家的记录按不存在处理。"""

        with self._session_factory() as db, db.begin():
            negotiation, approval, product, offer = self._get_owned_context(
                db,
                seller_id=seller_id,
                approval_id=approval_id,
                for_update=True,
            )
            if (
                approval.status is ApprovalStatus.PENDING
                and self._is_due(approval.expires_at)
            ):
                self._expire(db, negotiation=negotiation, approval=approval)
                db.flush()
                db.refresh(approval)
            return self._seller_snapshot(
                approval=approval,
                negotiation=negotiation,
                product=product,
                offer=offer,
            )

    def approve_request(
        self,
        *,
        seller_id: str,
        approval_id: int,
        request_id: str,
        comment: str | None = None,
    ) -> SellerApprovalSnapshot:
        """批准当前有效报价并创建待发送的后续通知任务。"""

        return self._review_request(
            seller_id=seller_id,
            approval_id=approval_id,
            request_id=request_id,
            comment=comment,
            target_status=ApprovalStatus.APPROVED,
        )

    def reject_request(
        self,
        *,
        seller_id: str,
        approval_id: int,
        request_id: str,
        comment: str | None = None,
    ) -> SellerApprovalSnapshot:
        """拒绝当前有效报价并恢复会话的可协商状态。"""

        return self._review_request(
            seller_id=seller_id,
            approval_id=approval_id,
            request_id=request_id,
            comment=comment,
            target_status=ApprovalStatus.REJECTED,
        )

    def cancel_request(
        self,
        *,
        session_id: int,
        buyer_id: str,
        approval_id: int,
    ) -> ApprovalSnapshot:
        """取消当前买家的待审批请求，并恢复可协商状态。"""

        with self._session_factory() as db, db.begin():
            negotiation = self._get_buyer_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
                for_update=True,
            )
            approval = self._get_approval(
                db,
                approval_id=approval_id,
                session_id=session_id,
                for_update=True,
            )
            if approval.status is ApprovalStatus.CANCELLED:
                return self._snapshot(approval)
            if approval.status is not ApprovalStatus.PENDING:
                raise ApprovalConflictError("审批请求已经处理，不能取消")
            if self._is_due(approval.expires_at):
                self._expire(db, negotiation=negotiation, approval=approval)
            else:
                approval.status = ApprovalStatus.CANCELLED
                self._restore_active(negotiation)
            db.flush()
            return self._snapshot(approval)

    def cancel_pending_for_new_offer_in_transaction(
        self,
        *,
        db: Session,
        session_id: int,
        buyer_id: str,
    ) -> ApprovalSnapshot | None:
        """在聊天事务内撤销旧审批，为当前买家的新正式报价让路。"""

        negotiation = self._get_buyer_negotiation(
            db,
            session_id=session_id,
            buyer_id=buyer_id,
            for_update=True,
        )
        approval = self._get_pending(db, session_id=session_id, for_update=True)
        if approval is None:
            if negotiation.status is NegotiationStatus.WAITING_APPROVAL:
                raise ApprovalConflictError("等待审批的会话缺少待处理审批记录")
            return None

        if self._is_due(approval.expires_at):
            self._expire(db, negotiation=negotiation, approval=approval)
        else:
            approval.status = ApprovalStatus.CANCELLED
            self._restore_active(negotiation)
        db.flush()
        return self._snapshot(approval)

    def expire_request(
        self,
        *,
        approval_id: int,
        now: datetime | None = None,
    ) -> ApprovalSnapshot:
        """由受控调用方将已经到期的待审批请求标记为失效。"""

        comparison_time = self._as_database_datetime(now or datetime.now(UTC))
        with self._session_factory() as db, db.begin():
            session_id = db.scalar(
                select(ApprovalRequest.session_id).where(
                    ApprovalRequest.id == approval_id
                )
            )
            if session_id is None:
                raise ApprovalNotFoundError("审批请求不存在")
            negotiation = db.get(
                NegotiationSession,
                session_id,
                with_for_update=True,
            )
            if negotiation is None:
                raise ApprovalNotFoundError("审批所属会话不存在")
            approval = self._get_approval(
                db,
                approval_id=approval_id,
                session_id=session_id,
                for_update=True,
            )
            if approval.status is ApprovalStatus.EXPIRED:
                return self._snapshot(approval)
            if approval.status is not ApprovalStatus.PENDING:
                raise ApprovalConflictError("只有待审批请求可以标记过期")
            if approval.expires_at > comparison_time:
                raise ApprovalNotExpiredError("审批请求尚未到期")
            self._expire(db, negotiation=negotiation, approval=approval)
            db.flush()
            return self._snapshot(approval)

    def expire_due_requests(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> tuple[ApprovalSnapshot, ...]:
        """批量扫描到期记录；逐条事务处理以缩小锁定范围。"""

        if not 1 <= limit <= 1000:
            raise ValueError("单次过期处理数量必须在 1 到 1000 之间")
        comparison_time = self._as_database_datetime(now or datetime.now(UTC))
        with self._session_factory() as db:
            approval_ids = tuple(
                db.scalars(
                    select(ApprovalRequest.id)
                    .where(
                        ApprovalRequest.status == ApprovalStatus.PENDING,
                        ApprovalRequest.expires_at <= comparison_time,
                    )
                    .order_by(ApprovalRequest.id)
                    .limit(limit)
                )
            )
        expired: list[ApprovalSnapshot] = []
        for approval_id in approval_ids:
            try:
                expired.append(
                    self.expire_request(
                        approval_id=approval_id,
                        now=comparison_time,
                    )
                )
            except (ApprovalConflictError, ApprovalNotFoundError):
                # 其他事务可能已经处理或删除了同一记录，继续扫描剩余任务。
                continue
        return tuple(expired)

    def _review_request(
        self,
        *,
        seller_id: str,
        approval_id: int,
        request_id: str,
        comment: str | None,
        target_status: ApprovalStatus,
    ) -> SellerApprovalSnapshot:
        stored_request_id = self._validated_request_id(request_id)
        stored_comment = self._validated_comment(comment)
        conflict_message: str | None = None
        result: SellerApprovalSnapshot | None = None

        with self._session_factory() as db, db.begin():
            negotiation, approval, product, offer = self._get_owned_context(
                db,
                seller_id=seller_id,
                approval_id=approval_id,
                for_update=True,
            )
            if (
                approval.status is target_status
                and approval.followup_request_id == stored_request_id
            ):
                return self._seller_snapshot(
                    approval=approval,
                    negotiation=negotiation,
                    product=product,
                    offer=offer,
                )
            if approval.status is not ApprovalStatus.PENDING:
                raise ApprovalConflictError("审批请求已经处理，不能重复变更结果")

            policy = db.scalar(
                select(SellerPolicy)
                .where(SellerPolicy.product_id == product.id)
                .with_for_update()
            )
            conflict_message = self._review_conflict(
                negotiation=negotiation,
                approval=approval,
                product=product,
                offer=offer,
                policy=policy,
            )
            if conflict_message is not None:
                # 已经过期或不再匹配真实业务状态的审批直接失效，避免继续占用会话。
                approval.status = ApprovalStatus.EXPIRED
                self._restore_active(negotiation)
                db.flush()
            else:
                approval.status = target_status
                approval.seller_comment = stored_comment
                approval.reviewed_at = datetime.now()
                approval.followup_status = ApprovalFollowupStatus.PENDING
                approval.followup_request_id = stored_request_id
                if target_status is ApprovalStatus.REJECTED:
                    offer.status = OfferStatus.REJECTED
                    self._restore_active(negotiation)
                else:
                    # 批准只记录卖家授权，等待阶段六通知买家，不直接形成成交。
                    negotiation.version += 1
                try:
                    db.flush()
                except IntegrityError as exc:
                    raise ApprovalConflictError("审批幂等键已经被其他请求使用") from exc
            db.refresh(approval)
            result = self._seller_snapshot(
                approval=approval,
                negotiation=negotiation,
                product=product,
                offer=offer,
            )

        if conflict_message is not None:
            raise ApprovalConflictError(conflict_message)
        if result is None:  # pragma: no cover - 防御不可达分支
            raise ApprovalConflictError("审批结果未能保存")
        return result

    def _expire_due_owned(self, db: Session, *, seller_id: str) -> None:
        now = datetime.now()
        due_items = tuple(
            db.execute(
                select(ApprovalRequest.id, ApprovalRequest.session_id)
                .join(
                    NegotiationSession,
                    NegotiationSession.id == ApprovalRequest.session_id,
                )
                .join(Product, Product.id == NegotiationSession.product_id)
                .where(
                    Product.seller_id == seller_id,
                    ApprovalRequest.status == ApprovalStatus.PENDING,
                    ApprovalRequest.expires_at <= now,
                )
                .order_by(ApprovalRequest.id)
            )
        )
        for approval_id, session_id in due_items:
            negotiation = db.get(
                NegotiationSession,
                session_id,
                with_for_update=True,
            )
            approval = db.get(ApprovalRequest, approval_id, with_for_update=True)
            if (
                negotiation is None
                or approval is None
                or approval.status is not ApprovalStatus.PENDING
                or not self._is_due(approval.expires_at)
            ):
                continue
            self._expire(db, negotiation=negotiation, approval=approval)
        db.flush()

    def _review_conflict(
        self,
        *,
        negotiation: NegotiationSession,
        approval: ApprovalRequest,
        product: Product,
        offer: Offer,
        policy: SellerPolicy | None,
    ) -> str | None:
        if ApprovalService._is_due(approval.expires_at):
            return "审批请求已经过期"
        if negotiation.status is not NegotiationStatus.WAITING_APPROVAL:
            return "协商会话已不再等待此审批"
        if product.status is not ProductStatus.AVAILABLE:
            return "商品当前不可协商，审批已经失效"
        if negotiation.current_offer_id != offer.id:
            return "买家已经提交更新的交易条件，审批已经失效"
        if offer.proposer is not OfferProposer.BUYER:
            return "审批关联的不是买家报价"
        if offer.status is not OfferStatus.PROPOSED:
            return "审批关联的报价已经处理"
        if offer.expires_at is not None and ApprovalService._is_due(offer.expires_at):
            return "审批关联的报价已经过期"
        if policy is None or policy.version != approval.policy_version:
            return "卖家规则版本已经变化，审批已经失效"
        # 这里不能信任申请时的区间结果，卖家操作前必须按数据库事实再次计算。
        current_authorization = self._negotiation_service.authorize_stored_offer(
            offer=offer,
            policy=policy,
        )
        if not current_authorization.can_request_approval:
            return "当前报价已不在人工审批区"
        return None

    @staticmethod
    def _get_owned_context(
        db: Session,
        *,
        seller_id: str,
        approval_id: int,
        for_update: bool,
    ) -> tuple[NegotiationSession, ApprovalRequest, Product, Offer]:
        session_id = db.scalar(
            select(ApprovalRequest.session_id)
            .join(
                NegotiationSession,
                NegotiationSession.id == ApprovalRequest.session_id,
            )
            .join(Product, Product.id == NegotiationSession.product_id)
            .where(
                ApprovalRequest.id == approval_id,
                Product.seller_id == seller_id,
            )
        )
        if session_id is None:
            raise ApprovalNotFoundError("审批请求不存在")
        negotiation = db.get(
            NegotiationSession,
            session_id,
            with_for_update=for_update,
        )
        approval = db.get(
            ApprovalRequest,
            approval_id,
            with_for_update=for_update,
        )
        if negotiation is None or approval is None:
            raise ApprovalNotFoundError("审批请求不存在")
        product = db.get(
            Product,
            negotiation.product_id,
            with_for_update=for_update,
        )
        offer = db.get(
            Offer,
            approval.offer_id,
            with_for_update=for_update,
        )
        if product is None or offer is None or offer.session_id != negotiation.id:
            raise ApprovalNotFoundError("审批关联的业务数据不存在")
        return negotiation, approval, product, offer

    @staticmethod
    def _get_buyer_negotiation(
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

    @staticmethod
    def _get_policy(
        db: Session,
        *,
        product_id: int,
        for_update: bool = False,
    ) -> SellerPolicy:
        statement = select(SellerPolicy).where(
            SellerPolicy.product_id == product_id
        )
        if for_update:
            statement = statement.with_for_update()
        policy = db.scalar(statement)
        if policy is None:
            raise PricingPolicyNotFoundError("商品尚未配置卖家规则")
        return policy

    @staticmethod
    def _get_pending(
        db: Session,
        *,
        session_id: int,
        for_update: bool = False,
    ) -> ApprovalRequest | None:
        statement = select(ApprovalRequest).where(
            ApprovalRequest.session_id == session_id,
            ApprovalRequest.status == ApprovalStatus.PENDING,
        )
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    @staticmethod
    def _get_approval(
        db: Session,
        *,
        approval_id: int,
        session_id: int,
        for_update: bool = False,
    ) -> ApprovalRequest:
        statement = select(ApprovalRequest).where(
            ApprovalRequest.id == approval_id,
            ApprovalRequest.session_id == session_id,
        )
        if for_update:
            statement = statement.with_for_update()
        approval = db.scalar(statement)
        if approval is None:
            raise ApprovalNotFoundError("审批请求不存在")
        return approval

    @staticmethod
    def _validate_offer(
        offer: Offer,
        *,
        approval_expires_at: datetime,
    ) -> None:
        if offer.proposer is not OfferProposer.BUYER:
            raise ApprovalNotAuthorizedError("只能为买家报价申请审批")
        if offer.status is not OfferStatus.PROPOSED:
            raise ApprovalConflictError("报价已失效或已处理")
        if offer.expires_at is not None:
            offer_expiration = ApprovalService._as_database_datetime(
                offer.expires_at
            )
            if ApprovalService._is_due(offer_expiration):
                raise ApprovalConflictError("报价已经过期")
            if approval_expires_at > offer_expiration:
                raise ApprovalConflictError("审批有效期不能晚于报价有效期")

    @staticmethod
    def _expire(
        db: Session,
        *,
        negotiation: NegotiationSession,
        approval: ApprovalRequest,
    ) -> None:
        approval.status = ApprovalStatus.EXPIRED
        ApprovalService._restore_active(negotiation)
        db.add_all([approval, negotiation])

    @staticmethod
    def _restore_active(negotiation: NegotiationSession) -> None:
        if negotiation.status is NegotiationStatus.WAITING_APPROVAL:
            negotiation.status = NegotiationStatus.ACTIVE
            negotiation.version += 1

    @staticmethod
    def _validated_reason(reason: str) -> str:
        stored = reason.strip()
        if not 1 <= len(stored) <= 2000:
            raise ApprovalConflictError("审批原因长度必须在 1 到 2000 个字符之间")
        return stored

    @staticmethod
    def _validated_request_id(request_id: str) -> str:
        stored = request_id.strip()
        if not 1 <= len(stored) <= 64 or _REQUEST_ID_PATTERN.fullmatch(stored) is None:
            raise ApprovalConflictError("审批幂等键格式不正确")
        return stored

    @staticmethod
    def _validated_comment(comment: str | None) -> str | None:
        if comment is None:
            return None
        stored = comment.strip()
        if len(stored) > 2000:
            raise ApprovalConflictError("卖家意见不能超过 2000 个字符")
        return stored or None

    @staticmethod
    def _as_database_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(microsecond=0)
        # MySQL DATETIME 不保存时区；统一转为运行环境本地时间后再去掉时区。
        return value.astimezone().replace(tzinfo=None, microsecond=0)

    @staticmethod
    def _is_due(value: datetime) -> bool:
        return ApprovalService._as_database_datetime(value) <= datetime.now()

    @staticmethod
    def _snapshot(approval: ApprovalRequest) -> ApprovalSnapshot:
        return ApprovalSnapshot(
            id=approval.id,
            session_id=approval.session_id,
            offer_id=approval.offer_id,
            policy_version=approval.policy_version,
            status=approval.status,
            reason=approval.reason,
            seller_comment=approval.seller_comment,
            expires_at=approval.expires_at,
            reviewed_at=approval.reviewed_at,
            followup_status=approval.followup_status,
            followup_request_id=approval.followup_request_id,
            created_at=approval.created_at,
            updated_at=approval.updated_at,
        )

    @staticmethod
    def _seller_snapshot(
        *,
        approval: ApprovalRequest,
        negotiation: NegotiationSession,
        product: Product,
        offer: Offer,
    ) -> SellerApprovalSnapshot:
        return SellerApprovalSnapshot(
            id=approval.id,
            session_id=approval.session_id,
            product_id=product.id,
            product_title=product.title,
            offer_id=approval.offer_id,
            policy_version=approval.policy_version,
            status=approval.status,
            reason=approval.reason,
            seller_comment=approval.seller_comment,
            expires_at=approval.expires_at,
            reviewed_at=approval.reviewed_at,
            followup_status=approval.followup_status,
            followup_request_id=approval.followup_request_id,
            created_at=approval.created_at,
            updated_at=approval.updated_at,
            session_status=negotiation.status,
            current_offer_id=negotiation.current_offer_id,
            offer=OfferSnapshot(
                id=offer.id,
                proposer=offer.proposer,
                price=offer.price,
                shipping_paid_by=offer.shipping_paid_by,
                shipping_cost=offer.shipping_cost,
                seller_borne_discount=offer.seller_borne_discount,
                additional_terms=dict(offer.terms),
                status=offer.status,
                expires_at=offer.expires_at,
                created_at=offer.created_at,
            ),
        )
