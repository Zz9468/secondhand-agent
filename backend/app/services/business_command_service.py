"""唯一事务所有者；回执、通知义务和 Outbox 与业务变更原子提交。"""

from datetime import timedelta

from sqlalchemy import select

from app.db.models import NegotiationSession, NegotiationStatus, Product, SellerPolicy
from app.db.models.graph import BusinessActionReceipt, NotificationDelivery, OutboxEvent
from app.runtime.contracts import Conflict, TypedInput, digest, thread_id, utcnow
from app.runtime.input_queue import bounded_transaction, lock_owner
from app.services.graph_business_operations import (
    confirm_in_transaction,
    lifecycle_in_transaction,
    resolve_offer_in_transaction,
)
from app.services.pricing_service import PricingError


def notification_in_transaction(db, run, result, now):
    source = f"turn:{run.id}:reply"
    db.add(
        NotificationDelivery(
            id=source,
            input_id=run.id,
            source_thread_id=thread_id(run.session_id),
            result=result,
            reply_deadline=now + timedelta(seconds=45),
        )
    )
    db.flush()
    return source


class BusinessCommandService:
    def __init__(self, factory, *, clock=utcnow, fault=lambda point: None):
        self.factory, self.clock, self.fault = factory, clock, fault

    def execute(self, run_id, owner, expected_version):
        with self.factory() as db, db.begin():
            bounded_transaction(db)
            now = self.clock()
            run, _ = lock_owner(db, run_id, owner, now)
            session = db.get(NegotiationSession, run.session_id, with_for_update=True)
            key = f"turn:{run_id}:action"
            command_hash = digest(
                {"input_hash": run.input_hash, "expected_version": expected_version}
            )
            receipt = db.get(BusinessActionReceipt, key)
            if receipt:
                if receipt.command_hash != command_hash:
                    raise Conflict("动作幂等键载荷冲突")
                return receipt.result
            if session.version != expected_version:
                raise Conflict("业务版本已改变")
            product = db.get(Product, session.product_id, with_for_update=True)
            policy = db.scalar(
                select(SellerPolicy)
                .where(
                    SellerPolicy.product_id == product.id,
                )
                .with_for_update()
            )
            payload = TypedInput.model_validate(run.payload)
            if session.status in {NegotiationStatus.AGREED, NegotiationStatus.CLOSED}:
                raise Conflict("会话已经结束")
            if payload.kind in {"SUBMIT", "REPLACE"}:
                result = resolve_offer_in_transaction(
                    db,
                    session,
                    product,
                    policy,
                    payload,
                    now,
                    self.fault,
                )
            elif payload.kind == "CONFIRM":
                result = confirm_in_transaction(db, session, product, policy, payload, run, now)
            elif payload.kind in {"WITHDRAW", "CLOSE"}:
                result = lifecycle_in_transaction(db, session, payload)
            else:
                raise Conflict("查询不是业务命令")
            result = {**result, "business_version": session.version}
            db.add(
                BusinessActionReceipt(
                    id=key,
                    input_id=run_id,
                    command_hash=command_hash,
                    result=result,
                    before_version=expected_version,
                    after_version=session.version,
                    committed_at=now,
                )
            )
            db.flush()
            self.fault("receipt")
            source = notification_in_transaction(db, run, result, now)
            self.fault("notification")
            db.add(
                OutboxEvent(
                    id=source, delivery_id=source, payload={"schema": 1, "delivery_id": source}
                )
            )
            db.flush()
            self.fault("outbox")
            return result

    def abort_or_query(self, run_id, owner, *, reason=None):
        """合法无动作结果也建立同源通知；永远不覆盖已提交业务。"""
        with self.factory() as db, db.begin():
            run, _ = lock_owner(db, run_id, owner, self.clock())
            session = db.get(NegotiationSession, run.session_id, with_for_update=True)
            source = f"turn:{run_id}:reply"
            delivery = db.get(NotificationDelivery, source)
            if delivery:
                return delivery.result
            product = db.get(Product, session.product_id)
            result = (
                {"outcome": "NOT_EXECUTED", "reason": reason}
                if reason
                else {
                    "outcome": "PRODUCT_INFO",
                    "title": product.title,
                    "description": product.description,
                }
            )
            notification_in_transaction(db, run, result, self.clock())
            db.add(
                OutboxEvent(
                    id=source, delivery_id=source, payload={"schema": 1, "delivery_id": source}
                )
            )
            return result


BUSINESS_ERRORS = (Conflict, PricingError)
