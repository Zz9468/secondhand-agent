"""跨重启调用预留和结果重放；调用在事务外，迟到产物只作审计。"""

from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from app.db.models.graph import ModelCallLedger, NegotiationInput
from app.runtime.contracts import BudgetExhausted, Conflict, StaleOwner, digest, utcnow
from app.runtime.input_queue import lock_owner


class ModelCallLedgerService:
    def __init__(self, factory, *, clock=utcnow):
        self.factory, self.clock = factory, clock

    def call(self, run_id, owner, step_id, invoke, *, reserved_tokens, reserved_cost=None):
        if reserved_tokens <= 0 or (reserved_cost is not None and reserved_cost < 0):
            raise Conflict("调用预算预留无效")
        with self.factory() as db, db.begin():
            run, _ = lock_owner(db, run_id, owner, self.clock())
            key = digest([run_id, step_id, run.input_hash, run.config_hash])
            old = db.scalar(select(ModelCallLedger).where(ModelCallLedger.accepted_key == key))
            if old:
                if digest(old.value) != old.value_hash:
                    raise Conflict("模型产物内容哈希不一致")
                return {"artifact_id": old.id, "value_hash": old.value_hash}
            calls = list(
                db.scalars(select(ModelCallLedger).where(ModelCallLedger.input_id == run_id))
            )
            tokens = sum(
                c.actual_tokens if c.actual_tokens is not None else c.reserved_tokens for c in calls
            )
            costs = [c.actual_cost if c.actual_cost is not None else c.reserved_cost for c in calls]
            cap = run.config.get("max_cost")
            if (
                len(calls) >= run.config["max_calls"]
                or tokens + reserved_tokens > run.config["max_tokens"]
                or (
                    cap is not None
                    and (
                        reserved_cost is None
                        or None in costs
                        or sum(costs, Decimal(0)) + reserved_cost > Decimal(str(cap))
                    )
                )
            ):
                raise BudgetExhausted("调用预算耗尽")
            attempt_id = uuid4().hex
            db.add(
                ModelCallLedger(
                    id=attempt_id,
                    input_id=run_id,
                    step_key=key,
                    ownership_epoch=owner[1],
                    status="RESERVED",
                    reserved_tokens=reserved_tokens,
                    reserved_cost=reserved_cost,
                    created_at=self.clock(),
                )
            )
        # callback 返回 (已验证结构, 实际 token 或 None, 实际费用或 None)。SDK 不得隐式重试。
        try:
            value, actual_tokens, actual_cost = invoke()
            value_hash = digest(value)
            if not isinstance(value, dict) or (actual_tokens is not None and actual_tokens < 0):
                raise Conflict("模型产物或用量无效")
            if actual_cost is not None and actual_cost < 0:
                raise Conflict("模型费用无效")
        except Exception:
            with self.factory() as db, db.begin():
                attempt = db.get(ModelCallLedger, attempt_id, with_for_update=True)
                attempt.status = "UNKNOWN"
            raise
        try:
            with self.factory() as db, db.begin():
                lock_owner(db, run_id, owner, self.clock())
                attempt = db.get(ModelCallLedger, attempt_id, with_for_update=True)
                attempt.value, attempt.value_hash = value, value_hash
                attempt.actual_tokens, attempt.actual_cost = actual_tokens, actual_cost
                attempt.accepted_key, attempt.status = key, "ACCEPTED"
        except StaleOwner:
            with self.factory() as db, db.begin():
                attempt = db.get(ModelCallLedger, attempt_id, with_for_update=True)
                attempt.status = "STALE"
                attempt.actual_tokens, attempt.actual_cost = actual_tokens, actual_cost
            raise
        return {"artifact_id": attempt_id, "value_hash": value_hash}

    def resolve(self, run_id, step_id, reference):
        with self.factory() as db:
            run = db.get(NegotiationInput, run_id)
            item = db.get(ModelCallLedger, reference["artifact_id"])
            key = digest([run_id, step_id, run.input_hash, run.config_hash])
            if (
                not item
                or item.accepted_key != key
                or item.status != "ACCEPTED"
                or item.value_hash != reference["value_hash"]
                or digest(item.value) != item.value_hash
            ):
                raise Conflict("模型产物引用无效")
            return item.value
