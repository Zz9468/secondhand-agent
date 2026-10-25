"""Composable lifecycle writes. Caller owns the transaction and session/product locks."""

from sqlalchemy import select

from app.db.models import (
    ApprovalRequest,
    ApprovalStatus,
    NegotiationStatus,
    OfferProposer,
    OfferStatus,
)
from app.db.models.graph import (
    ApprovalExecution,
    BusinessActionReceipt,
    NotificationDelivery,
    OutboxEvent,
)
from app.runtime.contracts import digest


def approval_thread(approval_id):
    return f"approval:{approval_id}:v1"


def event_in_transaction(db, approval, suffix, kind):
    key = f"approval:{approval.id}:{suffix}"
    if not db.get(OutboxEvent, key):
        db.add(
            OutboxEvent(
                id=key,
                approval_id=approval.id,
                payload={"schema": 1, "approval_id": approval.id, "kind": kind},
            )
        )
    return key


def start_approval(db, approval):
    approval.effect_status = "NOT_APPLIED"
    approval.effect_revision = 0
    db.add(ApprovalExecution(approval_id=approval.id))
    event_in_transaction(db, approval, "requested", "REQUESTED")


def effect_notice(db, session, approval, outcome, now):
    source = f"approval:{approval.id}:result:{approval.effect_revision}"
    if not db.get(NotificationDelivery, source):
        result = {
            "outcome": outcome,
            "offer_id": approval.offer_id,
            "approval_id": approval.id,
            "effect_revision": approval.effect_revision,
            "business_version": session.version,
            "invalid_reason": approval.invalid_reason,
        }
        db.add(
            NotificationDelivery(
                id=source,
                session_id=session.id,
                approval_id=approval.id,
                source_thread_id=approval_thread(approval.id),
                result=result,
                reply_deadline=now,
            )
        )
        db.flush()
        db.add(
            OutboxEvent(id=source, delivery_id=source, payload={"schema": 1, "delivery_id": source})
        )
        approval.notification_status = "PENDING"
    return source


def invalidate_approval(
    db, session, approval, now, reason, *, expired=False, before_version=None, after_version=None
):
    if approval.effect_status == "INVALIDATED":
        return
    if approval.status == ApprovalStatus.PENDING:
        approval.status = ApprovalStatus.EXPIRED if expired else ApprovalStatus.CANCELLED
    # Never erase APPROVED/REJECTED or reviewed_at/comment after a decision.
    approval.effect_status = "INVALIDATED"
    approval.effect_revision += 1
    approval.invalid_reason, approval.invalidated_at = reason, now
    key = f"approval:{approval.id}:invalidate:{approval.effect_revision}"
    db.add(
        BusinessActionReceipt(
            id=key,
            command_hash=digest([approval.id, approval.effect_revision, reason]),
            result={
                "outcome": "APPROVAL_INVALIDATED",
                "approval_id": approval.id,
                "offer_id": approval.offer_id,
                "invalid_reason": reason,
            },
            before_version=session.version if before_version is None else before_version,
            after_version=session.version if after_version is None else after_version,
            committed_at=now,
        )
    )
    event_in_transaction(db, approval, f"invalidated:{approval.effect_revision}", "INVALIDATED")
    effect_notice(db, session, approval, "APPROVAL_INVALIDATED", now)


def invalidate_offer(db, session, offer, now, reason):
    if not offer or offer.status not in {OfferStatus.PROPOSED, OfferStatus.ACCEPTED}:
        return
    offer.status = (
        OfferStatus.WITHDRAWN if offer.proposer == OfferProposer.BUYER else OfferStatus.REJECTED
    )
    offer.invalid_reason, offer.invalidated_at = reason, now
    current = session.current_offer_id == offer.id
    if current:
        session.current_offer_id = None
        if session.status == NegotiationStatus.WAITING_APPROVAL:
            session.status = NegotiationStatus.ACTIVE
    approvals = db.scalars(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.offer_id == offer.id,
        )
        .order_by(ApprovalRequest.id)
        .with_for_update()
    )
    for approval in approvals:
        # Buyer commands increment the session once later in this same transaction.
        # Reconciliation has already recorded its own invalidation and returns above.
        invalidate_approval(
            db,
            session,
            approval,
            now,
            reason,
            before_version=session.version,
            after_version=session.version + int(current),
        )
    db.flush()  # Release the pending-session unique key before creating the replacement.


def supersede(db, old, new):
    if old and old.invalid_reason == "REPLACED":
        old.superseded_by = new.id
        for approval in db.scalars(
            select(ApprovalRequest).where(ApprovalRequest.offer_id == old.id)
        ):
            approval.superseded_by = new.id


def close_drafts(db, session):
    from app.db.models.semantic import OfferDraft

    for draft in db.scalars(select(OfferDraft).where(OfferDraft.session_id == session.id)):
        if draft.status in {"PENDING", "READY"}:
            draft.status = "WITHDRAWN"


def public_approval_status(db, session, now):
    approval = db.scalar(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.session_id == session.id,
        )
        .order_by(ApprovalRequest.id.desc())
        .limit(1)
    )
    if not approval:
        text = "当前没有卖家审批记录。"
    elif approval.effect_status == "INVALIDATED":
        text = "原审批已失效，不能用于确认旧报价。"
    elif approval.status == ApprovalStatus.PENDING:
        text = (
            "审批响应期限已到，等待系统收尾。"
            if approval.expires_at <= now
            else "当前仍在等待卖家审批，尚未接受报价，无法承诺审批时间。"
        )
    elif approval.status == ApprovalStatus.APPROVED:
        text = (
            "卖家已批准，等待系统复核并应用结果，尚不能确认。"
            if approval.effect_status == "NOT_APPLIED"
            else "卖家批准已应用；请以当前有效方案卡片及最终确认结果为准。"
        )
    else:
        text = {
            ApprovalStatus.REJECTED: "卖家已拒绝该报价，可继续协商。",
            ApprovalStatus.CANCELLED: "原审批已取消。",
            ApprovalStatus.EXPIRED: "原审批已到期。",
        }[approval.status]
    return "ANSWERED", text, None
