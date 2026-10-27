"""Atomic seller decisions, ApplyApprovalResult and lifecycle reconciliation."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    ApprovalRequest,
    ApprovalStatus,
    NegotiationSession,
    NegotiationStatus,
    Offer,
    OfferStatus,
    Product,
    SellerPolicy,
)
from app.db.models.graph import (
    ApprovalExecution,
    BusinessActionReceipt,
    NotificationDelivery,
    OutboxEvent,
)
from app.runtime.contracts import Conflict, StaleOwner, digest, thread_id, utcnow
from app.runtime.input_queue import bounded_transaction
from app.services.approval_lifecycle import (
    effect_notice,
    event_in_transaction,
    invalidate_approval,
    invalidate_offer,
)
from app.services.approval_service import ApprovalService
from app.services.errors import ApprovalConflictError, ApprovalNotFoundError
from app.services.graph_business_operations import basis_valid


def lock_execution(db, approval_id, owner, now):
    execution = db.get(ApprovalExecution, approval_id, with_for_update=True)
    if (
        not execution
        or (execution.lease_token, execution.ownership_epoch) != owner
        or not execution.lease_until
        or execution.lease_until <= now
    ):
        raise StaleOwner("审批执行所有权已失效")
    return execution


def lock_context(db, approval_id, seller_id=None):
    sid = db.scalar(select(ApprovalRequest.session_id).where(ApprovalRequest.id == approval_id))
    session = db.get(NegotiationSession, sid, with_for_update=True) if sid else None
    if not session or not session.graph_version:
        raise ApprovalNotFoundError("审批请求不存在")
    product = db.get(Product, session.product_id, with_for_update=True)
    if seller_id is not None and product.seller_id != seller_id:
        raise ApprovalNotFoundError("审批请求不存在")
    policy = db.scalar(
        select(SellerPolicy)
        .where(
            SellerPolicy.product_id == product.id,
        )
        .with_for_update()
    )
    offer_id = db.scalar(select(ApprovalRequest.offer_id).where(ApprovalRequest.id == approval_id))
    offer = db.get(Offer, offer_id, with_for_update=True)
    approval = db.get(ApprovalRequest, approval_id, with_for_update=True, populate_existing=True)
    return session, product, policy, offer, approval


def reconcile_approval(db, session, product, policy, offer, approval, now):
    """Only the current object can change session state; final intent is immutable."""
    if session.status == NegotiationStatus.AGREED:
        return
    if approval.effect_status == "INVALIDATED" or approval.status == ApprovalStatus.REJECTED:
        return
    expired = approval.status == ApprovalStatus.PENDING and approval.expires_at <= now
    if expired or not basis_valid(db, session, product, policy, offer, now):
        reason = "APPROVAL_EXPIRED" if expired else "OFFER_BASIS_INVALID"
        before = session.version
        current = session.current_offer_id == offer.id
        if current:
            session.version += 1
        # Preserve EXPIRED rather than turning a pending timeout into CANCELLED.
        invalidate_approval(
            db, session, approval, now, reason, expired=expired, before_version=before
        )
        invalidate_offer(db, session, offer, now, reason)


class GraphApprovalService:
    def __init__(self, factory, *, clock=utcnow, fault=lambda point: None):
        self.factory, self.clock, self.fault = factory, clock, fault

    def get_request(self, seller_id, approval_id, request_id):
        with self.factory() as db:
            approval = db.get(ApprovalRequest, approval_id)
            session = db.get(NegotiationSession, approval.session_id) if approval else None
            product = db.get(Product, session.product_id) if session else None
            if not product or product.seller_id != seller_id:
                raise ApprovalNotFoundError("审批请求不存在")
            receipt = db.get(
                BusinessActionReceipt, digest(["seller-approval", seller_id, request_id])
            )
            if not receipt or receipt.result.get("approval_id") != approval_id:
                raise ApprovalNotFoundError("审批请求不存在")
            return {
                "status": "RECORDED",
                "approval_id": approval_id,
                "decision": receipt.result["decision"],
            }

    def review(self, *, seller_id, approval_id, request_id, comment, approved, expected_revision):
        request_id = ApprovalService._validated_request_id(request_id)
        comment = ApprovalService._validated_comment(comment)
        command_hash = digest([seller_id, approval_id, approved, comment, expected_revision])
        key = digest(["seller-approval", seller_id, request_id])
        conflict = None
        try:
            with self.factory() as db, db.begin():
                bounded_transaction(db)
                now = self.clock()
                session, product, policy, offer, approval = lock_context(db, approval_id, seller_id)
                old = db.get(BusinessActionReceipt, key)
                if old:
                    if old.command_hash != command_hash:
                        raise ApprovalConflictError("审批幂等键载荷冲突")
                else:
                    if expected_revision is None or expected_revision != approval.effect_revision:
                        raise ApprovalConflictError("审批效力版本已改变，请刷新后操作")
                    if approval.status != ApprovalStatus.PENDING:
                        raise ApprovalConflictError("审批请求已经处理")
                    before = session.version
                    reconcile_approval(db, session, product, policy, offer, approval, now)
                    if approval.effect_status == "INVALIDATED":
                        conflict = "审批已到期或关联报价依据已失效"
                    else:
                        approval.status = (
                            ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
                        )
                        approval.reviewed_at, approval.seller_comment = now, comment
                        approval.followup_request_id, approval.decision_hash = (
                            key,
                            command_hash,
                        )
                        event_in_transaction(db, approval, "decision", "DECIDED")
                        self.fault("decision")
                        if not approved:
                            offer.status = OfferStatus.REJECTED
                            offer.invalid_reason, offer.invalidated_at = "SELLER_REJECTED", now
                            if session.current_offer_id == offer.id:
                                session.current_offer_id = None
                                session.status = NegotiationStatus.ACTIVE
                                session.version += 1
                            approval.effect_status = "APPLIED"
                            approval.effect_revision += 1
                        db.flush()
                        self.fault("effect")
                        db.add(
                            BusinessActionReceipt(
                                id=key,
                                command_hash=command_hash,
                                result={
                                    "approval_id": approval.id,
                                    "decision": approval.status.value,
                                },
                                before_version=before,
                                after_version=session.version,
                                committed_at=now,
                            )
                        )
                        db.flush()
                        self.fault("receipt")
                        if not approved:
                            effect_notice(db, session, approval, "APPROVAL_REJECTED", now)
                        db.flush()
                        self.fault("notification")
                        self.fault("outbox")
                db.flush()
                result = ApprovalService._seller_snapshot(
                    approval=approval,
                    negotiation=session,
                    product=product,
                    offer=offer,
                )
        except IntegrityError as exc:
            raise ApprovalConflictError("审批幂等键已经使用") from exc
        if conflict:
            raise ApprovalConflictError(conflict)
        return result

    def apply(self, approval_id, event_id, owner):
        with self.factory() as db, db.begin():
            bounded_transaction(db)
            now = self.clock()
            lock_execution(db, approval_id, owner, now)
            session, product, policy, offer, approval = lock_context(db, approval_id)
            event = db.get(OutboxEvent, event_id)
            if (
                not event
                or event.approval_id != approval_id
                or event.delivery_id is not None
                or event.payload
                != {"schema": 1, "approval_id": approval_id, "kind": event.payload.get("kind")}
                or event.payload.get("kind") not in {"DECIDED", "INVALIDATED"}
            ):
                raise Conflict("审批事件引用无效")
            key = f"approval:{approval_id}:apply:{digest(event_id)[:20]}"
            command_hash = digest([approval_id, event_id, event.payload])
            receipt = db.get(BusinessActionReceipt, key)
            if receipt:
                if receipt.command_hash != command_hash:
                    raise Conflict("审批事件载荷冲突")
                return receipt.result
            before = session.version
            reconcile_approval(db, session, product, policy, offer, approval, now)
            if approval.status == ApprovalStatus.PENDING:
                raise Conflict("审批尚无真实终态事件")
            if (
                approval.status == ApprovalStatus.APPROVED
                and approval.effect_status == "NOT_APPLIED"
            ):
                if not approval.reviewed_at or approval.reviewed_at >= approval.expires_at:
                    raise Conflict("缺少响应期限内的真实卖家决定")
                # Timely decisions remain applicable past response deadline, subject to basis.
                offer.status = OfferStatus.ACCEPTED
                session.status = NegotiationStatus.ACTIVE
                session.version += 1
                approval.effect_status = "APPLIED"
                approval.effect_revision += 1
            db.flush()
            self.fault("effect")
            outcome = (
                "APPROVAL_INVALIDATED"
                if approval.effect_status == "INVALIDATED"
                else "OFFER_ACCEPTED"
                if approval.status == ApprovalStatus.APPROVED
                else "APPROVAL_REJECTED"
            )
            result = {
                "outcome": outcome,
                "approval_id": approval_id,
                "effect_revision": approval.effect_revision,
            }
            db.add(
                BusinessActionReceipt(
                    id=key,
                    command_hash=command_hash,
                    result=result,
                    before_version=before,
                    after_version=session.version,
                    committed_at=now,
                )
            )
            db.flush()
            self.fault("receipt")
            effect_notice(db, session, approval, outcome, now)
            db.flush()
            self.fault("notification")
            self.fault("outbox")
            return result

    def reconcile(self, approval_id):
        with self.factory() as db, db.begin():
            bounded_transaction(db)
            context = lock_context(db, approval_id)
            reconcile_approval(db, *context, self.clock())

    def reconcile_offer(self, sid):
        with self.factory() as db, db.begin():
            bounded_transaction(db)
            session = db.get(NegotiationSession, sid, with_for_update=True)
            session.lifecycle_checked_at = self.clock()
            if (
                not session.graph_version
                or not session.current_offer_id
                or session.status in {NegotiationStatus.AGREED, NegotiationStatus.CLOSED}
            ):
                return
            product = db.get(Product, session.product_id, with_for_update=True)
            policy = db.scalar(
                select(SellerPolicy)
                .where(
                    SellerPolicy.product_id == product.id,
                )
                .with_for_update()
            )
            offer = db.get(Offer, session.current_offer_id, with_for_update=True)
            approval = db.scalar(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.offer_id == offer.id,
                )
                .with_for_update()
            )
            now = self.clock()
            if approval:
                reconcile_approval(db, session, product, policy, offer, approval, now)
            elif not basis_valid(db, session, product, policy, offer, now):
                before = session.version
                session.version += 1
                invalidate_offer(db, session, offer, now, "OFFER_BASIS_INVALID")
                source = f"offer:{offer.id}:invalidated"
                result = {
                    "outcome": "OFFER_INVALIDATED",
                    "offer_id": offer.id,
                    "business_version": session.version,
                }
                db.add(
                    BusinessActionReceipt(
                        id=source,
                        command_hash=digest([offer.id, "INVALIDATED"]),
                        result=result,
                        before_version=before,
                        after_version=session.version,
                        committed_at=now,
                    )
                )
                db.add(
                    NotificationDelivery(
                        id=source,
                        session_id=sid,
                        source_thread_id=thread_id(sid),
                        result=result,
                        reply_deadline=now,
                    )
                )
                db.flush()
                db.add(
                    OutboxEvent(
                        id=source, delivery_id=source, payload={"schema": 1, "delivery_id": source}
                    )
                )

    def claim(self, approval_id):
        with self.factory() as db, db.begin():
            bounded_transaction(db)
            row = db.get(ApprovalExecution, approval_id, with_for_update=True)
            now = self.clock()
            if (
                not row
                or row.recovery_required
                or (row.lease_until and row.lease_until > now)
                or (row.next_retry_at and row.next_retry_at > now)
            ):
                return None
            row.lease_token, row.lease_until = uuid4().hex, now + timedelta(seconds=30)
            row.ownership_epoch += 1
            return row.lease_token, row.ownership_epoch

    def release(self, approval_id, owner, *, checkpoint=None, failed=False, recovery=False):
        with self.factory() as db, db.begin():
            bounded_transaction(db)
            row = lock_execution(db, approval_id, owner, self.clock())
            if checkpoint:
                row.checkpoint_ref = checkpoint
            row.attempts = row.attempts + 1 if failed else 0
            row.recovery_required = recovery or row.attempts >= 3
            row.next_retry_at = (
                self.clock() + timedelta(seconds=2**row.attempts) if failed else None
            )
            row.lease_token = row.lease_until = None

    def pending_events(self, approval_id):
        with self.factory() as db:
            return list(
                db.scalars(
                    select(OutboxEvent.id)
                    .where(
                        OutboxEvent.approval_id == approval_id,
                        OutboxEvent.delivery_id.is_(None),
                        OutboxEvent.status == "PENDING",
                    )
                    .order_by(OutboxEvent.id)
                )
            )

    def terminal_event(self, approval_id):
        with self.factory() as db:
            approval = db.get(ApprovalRequest, approval_id)
            if approval.status == ApprovalStatus.PENDING:
                return None
            return db.scalar(
                select(OutboxEvent.id)
                .where(
                    OutboxEvent.approval_id == approval_id,
                    OutboxEvent.delivery_id.is_(None),
                    or_(OutboxEvent.id.like("%:decision"), OutboxEvent.id.like("%:invalidated:%")),
                )
                .order_by(OutboxEvent.id.desc())
            )

    def acknowledge(self, approval_id, events, owner):
        with self.factory() as db, db.begin():
            lock_execution(db, approval_id, owner, self.clock())
            for key in events:
                row = db.get(OutboxEvent, key, with_for_update=True)
                if row and row.approval_id == approval_id:
                    row.status = "DELIVERED"

    def covered_events(self, approval_id, result):
        """A later invalidation also settles an older decision delivered out of order."""
        covered = [f"approval:{approval_id}:requested", f"approval:{approval_id}:decision"]
        if result and result["outcome"] == "APPROVAL_INVALIDATED":
            for key in self.pending_events(approval_id):
                prefix = f"approval:{approval_id}:invalidated:"
                if key.startswith(prefix) and int(key[len(prefix) :]) <= result["effect_revision"]:
                    covered.append(key)
        return covered
