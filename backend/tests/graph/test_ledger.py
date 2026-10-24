from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models.graph import ModelCallLedger
from app.runtime.contracts import CONFIG, BudgetExhausted, Conflict, StaleOwner
from app.services.model_call_ledger_service import ModelCallLedgerService
from tests.graph.test_business import submit


def test_accepted_artifact_replay_and_reference_integrity(case):
    factory, sid, buyer, _, queue, _ = case
    run = queue.enqueue(sid, buyer, "one", submit())
    owner = queue.claim(run)
    ledger = ModelCallLedgerService(factory, clock=queue.clock)
    called = []

    def invoke():
        called.append(1)
        return {"intent": "SUBMIT"}, 12, Decimal("0.01")

    ref = ledger.call(
        run, owner, "semantic", invoke, reserved_tokens=100, reserved_cost=Decimal("0.1")
    )
    assert ledger.call(run, owner, "semantic", invoke, reserved_tokens=100) == ref
    assert called == [1]
    assert ledger.resolve(run, "semantic", ref) == {"intent": "SUBMIT"}
    with pytest.raises(Conflict):
        ledger.resolve(run, "other", ref)
    with factory() as db, db.begin():
        db.get(ModelCallLedger, ref["artifact_id"]).value = {"intent": "forged"}
    with pytest.raises(Conflict):
        ledger.resolve(run, "semantic", ref)


def test_unknown_usage_reservation_survives_restart_and_blocks_budget(case):
    factory, sid, buyer, clock, queue, _ = case
    run = queue.enqueue(sid, buyer, "one", submit(), config={**CONFIG, "max_calls": 1})
    owner = queue.claim(run)
    ledger = ModelCallLedgerService(factory, clock=clock)

    def timeout():
        raise TimeoutError("synthetic timeout")

    with pytest.raises(TimeoutError):
        ledger.call(run, owner, "semantic", timeout, reserved_tokens=100)
    queue.release(run, owner)
    clock.advance()
    owner = queue.claim(run)
    with pytest.raises(BudgetExhausted):
        ModelCallLedgerService(factory, clock=clock).call(
            run,
            owner,
            "semantic",
            lambda: pytest.fail("unexpected RPC"),
            reserved_tokens=1,
        )
    with factory() as db:
        item = db.scalar(select(ModelCallLedger).where(ModelCallLedger.input_id == run))
        assert (
            item.status == "UNKNOWN" and item.actual_tokens is None and item.reserved_tokens == 100
        )


def test_a33_late_old_owner_result_is_audit_only(case):
    factory, sid, buyer, clock, queue, _ = case
    run = queue.enqueue(sid, buyer, "one", submit())
    old = queue.claim(run)
    ledger = ModelCallLedgerService(factory, clock=clock)
    new = []

    def late():
        # 此处模拟撤销所有权；新的 Runner 仍必须另行取得线程锁。
        queue.release(run, old)
        clock.advance()
        new.append(queue.claim(run))
        return {"intent": "wrong-old-result"}, 20, None

    with pytest.raises(StaleOwner):
        ledger.call(run, old, "semantic", late, reserved_tokens=100)
    ref = ledger.call(
        run, new[0], "semantic", lambda: ({"intent": "current"}, 25, None), reserved_tokens=100
    )
    assert ledger.resolve(run, "semantic", ref) == {"intent": "current"}
    with factory() as db:
        items = list(db.scalars(select(ModelCallLedger).where(ModelCallLedger.input_id == run)))
        assert sorted(x.status for x in items) == ["ACCEPTED", "STALE"]
        assert len([x for x in items if x.accepted_key]) == 1


def test_cost_reservation_is_conservative(case):
    factory, sid, buyer, _, queue, _ = case
    run = queue.enqueue(sid, buyer, "one", submit(), config={**CONFIG, "max_cost": "0.15"})
    owner = queue.claim(run)
    ledger = ModelCallLedgerService(factory, clock=queue.clock)
    ledger.call(
        run,
        owner,
        "first",
        lambda: ({"ok": True}, None, None),
        reserved_tokens=100,
        reserved_cost=Decimal("0.1"),
    )
    with pytest.raises(BudgetExhausted):
        ledger.call(
            run,
            owner,
            "second",
            lambda: pytest.fail("unexpected RPC"),
            reserved_tokens=1,
            reserved_cost=Decimal("0.1"),
        )
