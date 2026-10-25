"""正常图和独立通知共享来源键、发布事务及展示上下文。"""

from datetime import timedelta
from secrets import token_urlsafe
from uuid import uuid4

from sqlalchemy import select

from app.db.models import (
    ApprovalRequest,
    Message,
    MessageRole,
    ModelExecutionTask,
    NegotiationSession,
    Offer,
    Product,
    SellerPolicy,
)
from app.db.models.graph import (
    DisplayAck,
    NegotiationInput,
    NotificationDelivery,
    OutboxEvent,
    PublicInteractionContext,
)
from app.runtime.contracts import Conflict, StaleOwner, utcnow
from app.runtime.input_queue import bounded_transaction, lock_owner, owned_session
from app.services.agent_message_service import AgentMessageService
from app.services.graph_business_operations import confirmation_expiry, terms_hash, valid_offer

TEXT = {
    "OFFER_ACCEPTED": "报价已接受，等待买家最终确认。",
    "WAITING_APPROVAL": "报价已提交，等待卖家审批，尚未接受。",
    "OFFER_REJECTED": "该报价未被接受。",
    "COUNTER_OFFERED": "已提出可确认的还价方案。",
    "INTENT_CONFIRMED": "已记录交易意向，不代表已付款或已完成交易。",
    "OFFER_WITHDRAWN": "报价已撤回。",
    "SESSION_CLOSED": "协商已关闭。",
    "APPROVAL_REJECTED": "卖家已拒绝该报价，可继续协商。",
    "APPROVAL_INVALIDATED": "该审批已到期或关联报价已失效，不能用于确认。",
    "OFFER_INVALIDATED": "该报价或其事实、成本、策略依据已失效，请重新协商。",
}


class NotificationService:
    def __init__(self, factory, *, clock=utcnow):
        self.factory, self.clock = factory, clock

    def claim(self, source):
        """仅在来源线程锁内调用；通知有独立租约，不运行旧图。"""
        with self.factory() as db, db.begin():
            bounded_transaction(db)
            item = db.get(NotificationDelivery, source)
            run = db.get(NegotiationInput, item.input_id) if item.input_id else None
            task = db.get(ModelExecutionTask, run.task_id, with_for_update=True) if run else None
            item = db.get(
                NotificationDelivery, source, with_for_update=True, populate_existing=True
            )
            now = self.clock()
            if item.status in {"PUBLISHED", "MANUAL_REQUIRED"}:
                return None
            if (
                (item.lease_until and item.lease_until > now)
                or (item.next_retry_at and item.next_retry_at > now)
                or (
                    task
                    and task.lease_expires_at
                    and task.lease_expires_at > now
                    and item.reply_deadline > now
                )
            ):
                return None
            item.lease_token = uuid4().hex
            item.lease_until = now + timedelta(seconds=15)
            item.status = "RUNNING"
            item.attempts += 1
            return item.lease_token

    def retry(self, source, token):
        with self.factory() as db, db.begin():
            item = db.get(NotificationDelivery, source, with_for_update=True)
            if item.lease_token != token:
                raise StaleOwner("通知所有权已失效")
            item.status = "MANUAL_REQUIRED" if item.attempts >= 3 else "RETRY_WAIT"
            item.next_retry_at = self.clock() + timedelta(seconds=2**item.attempts)
            item.lease_token = item.lease_until = None
            if item.approval_id:
                db.get(NegotiationSession, item.session_id, with_for_update=True)
                approval = db.get(ApprovalRequest, item.approval_id, with_for_update=True)
                if approval.effect_revision == item.result.get("effect_revision"):
                    approval.notification_status = item.status

    def publish(self, run_id, *, owner=None, notification_token=None, source=None):
        source = source or f"turn:{run_id}:reply"
        with self.factory() as db, db.begin():
            bounded_transaction(db)
            now = self.clock()
            if owner:
                run, _ = lock_owner(db, run_id, owner, now)
            else:
                run = db.get(NegotiationInput, run_id) if run_id else None
                item = db.get(NotificationDelivery, source, with_for_update=True)
                if (
                    not notification_token
                    or item.lease_token != notification_token
                    or not item.lease_until
                    or item.lease_until <= now
                ):
                    raise StaleOwner("通知所有权已失效")
            session = db.get(
                NegotiationSession, run.session_id if run else item.session_id, with_for_update=True
            )
            product = db.get(Product, session.product_id, with_for_update=True)
            policy = db.scalar(
                select(SellerPolicy)
                .where(
                    SellerPolicy.product_id == product.id,
                )
                .with_for_update()
            )
            item = db.get(NotificationDelivery, source, with_for_update=True)
            if item.status == "PUBLISHED":
                return item.public_message_id
            result = item.result
            offer = (
                db.get(Offer, result.get("offer_id"), with_for_update=True)
                if result.get("offer_id")
                else None
            )
            confirmable = (
                result["outcome"] in {"OFFER_ACCEPTED", "COUNTER_OFFERED"}
                and result.get("business_version") == session.version
                and valid_offer(db, session, product, policy, offer, now)
            )
            content = TEXT.get(result["outcome"], "本次未执行业务动作。")
            if result["outcome"] == "PRODUCT_INFO":
                content = f"{product.title}：{product.description}"
            if result["outcome"] == "NOT_EXECUTED":
                content += result["reason"] or ""
            historical = bool(
                offer
                and (
                    session.current_offer_id != offer.id
                    or session.version != result.get("business_version")
                    or offer.expires_at <= now
                    or (
                        result["outcome"] in {"OFFER_ACCEPTED", "COUNTER_OFFERED"}
                        and not confirmable
                    )
                )
            )
            if historical and result["outcome"] in {
                "OFFER_ACCEPTED",
                "COUNTER_OFFERED",
                "WAITING_APPROVAL",
                "APPROVAL_REJECTED",
                "APPROVAL_INVALIDATED",
                "OFFER_INVALIDATED",
            }:
                content = (
                    f"历史结果：{TEXT[result['outcome']]} 此结果已不是当前可操作方案；"
                    f"当前协商状态为 {session.status.value}，请查看最新结果。"
                )
            context_id = uuid4().hex if confirmable else None
            card = None
            if offer:
                card = {
                    "offer_id": offer.id,
                    "amount": f"{offer.price:.2f}",
                    "shipping_paid_by": offer.shipping_paid_by.value,
                    "terms": offer.terms,
                    "can_confirm": confirmable,
                    "context_id": context_id,
                    "expires_at": offer.expires_at.isoformat() + "Z",
                }
            from app.semantic.presentation import decorate

            draft_refs = None
            if run:
                content, card, draft_refs = decorate(db, run, session, result, content, card, now)
            if draft_refs and card and card.get("kind") == "drafts":
                context_id = card["context_id"]
            message = Message(
                session_id=session.id,
                role=MessageRole.AGENT,
                source_id=source,
                request_id=source,
                content=content,
                public_seq=session.next_public_seq,
                card=card,
                formal_offer_id=offer.id if offer else None,
            )
            session.next_public_seq += 1
            db.add(message)
            db.flush()
            if context_id:
                db.add(
                    PublicInteractionContext(
                        id=context_id,
                        session_id=session.id,
                        buyer_id=session.buyer_id,
                        message_id=message.id,
                        public_seq=message.public_seq,
                        offer_id=offer.id if offer else None,
                        terms_hash=terms_hash(offer) if offer else "drafts-v1",
                        draft_refs=draft_refs,
                        business_version=session.version,
                        expires_at=confirmation_expiry(db, offer, now)
                        if offer
                        else now + timedelta(minutes=30),
                    )
                )
            AgentMessageService.project(db, message, kind="GRAPH_RESULT")
            item.status = "PUBLISHED"
            item.public_message_id = message.id
            item.lease_token = item.lease_until = None
            event = db.get(OutboxEvent, source, with_for_update=True)
            event.status = "DELIVERED"
            if item.approval_id:
                approval = db.get(ApprovalRequest, item.approval_id, with_for_update=True)
                if approval.effect_revision == result.get("effect_revision"):
                    approval.notification_status = "PUBLISHED"
            return message.id

    def display(self, sid, buyer_id, context_id, view_id, request_id):
        if not view_id or not request_id or len(view_id) > 64 or len(request_id) > 64:
            raise Conflict("展示请求标识无效")
        with self.factory() as db, db.begin():
            session = owned_session(db, sid, buyer_id, lock=True)
            old = db.scalar(
                select(DisplayAck).where(
                    DisplayAck.buyer_id == buyer_id,
                    DisplayAck.client_view_id == view_id,
                    DisplayAck.request_id == request_id,
                )
            )
            if old:
                if old.context_id != context_id:
                    raise Conflict("展示请求载荷冲突")
                return old.id
            context = db.get(PublicInteractionContext, context_id)
            if (
                not context
                or context.session_id != sid
                or context.buyer_id != buyer_id
                or context.expires_at <= self.clock()
                or context.business_version != session.version
            ):
                raise Conflict("展示上下文已失效或无权访问")
            from app.semantic.presentation import check_object

            check_object(db, context, session, self.clock())
            ack = DisplayAck(
                id=token_urlsafe(32),
                context_id=context_id,
                buyer_id=buyer_id,
                client_view_id=view_id,
                request_id=request_id,
                displayed_at=self.clock(),
            )
            db.add(ack)
            return ack.id
