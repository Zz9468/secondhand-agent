"""P3 lifecycle acceptance using real MySQL and the official PostgreSQL checkpointer."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.dependencies import get_current_user, get_session_factory_dependency
from app.db.models import (
    ApprovalRequest,
    Message,
    NegotiationSession,
    Offer,
    Product,
    SellerPolicy,
)
from app.db.models.graph import (
    ApprovalExecution,
    BusinessActionReceipt,
    NotificationDelivery,
    OutboxEvent,
    PublicInteractionContext,
)
from app.db.models.semantic import CostQuote, SemanticOfferBasis
from app.main import create_app
from app.persistence.checkpointer import ThreadBusy, locked_saver
from app.runtime.approval_runner import ApprovalRunner
from app.runtime.contracts import Conflict, StaleOwner, Terms, TypedInput
from app.runtime.recovery import RecoveryConflict
from app.services.approval_lifecycle import approval_thread
from app.services.errors import ApprovalConflictError
from app.services.graph_approval_service import GraphApprovalService
from app.services.notification_service import NotificationService
from app.workers.graph_worker import run_batch
from tests.graph.test_business import receipt, submit
from tests.graph.test_display_queue import confirm
from tests.graph.test_semantic import (
    candidate,
    fact,
    intent,
    result,
    rule,
    semantic_case,  # noqa: F401
    turn,
)


def pending(case, amount="800"):
    factory, sid, buyer, clock, queue, runner = case
    run = queue.enqueue(sid, buyer, uuid4().hex, submit(amount))
    runner.run(run)
    outcome = receipt(factory, run).result
    with factory() as db:
        seller = db.get(Product, db.get(NegotiationSession, sid).product_id).seller_id
    return SimpleNamespace(
        id=outcome["approval_id"],
        offer=outcome["offer_id"],
        seller=seller,
        commands=GraphApprovalService(factory, clock=clock),
        runner=ApprovalRunner(factory, os.environ["P0_POSTGRES_URL"], clock=clock),
    )


def review(p, approved=True, **kwargs):
    return p.commands.review(
        seller_id=p.seller,
        approval_id=p.id,
        approved=approved,
        request_id=kwargs.pop("request_id", uuid4().hex),
        comment=kwargs.pop("comment", None),
        expected_revision=kwargs.pop("expected_revision", 0),
        **kwargs,
    )


def approval_card(case, p):
    factory, sid, buyer, clock, _, _ = case
    with factory() as db:
        message = db.scalar(select(Message).where(Message.source_id == f"approval:{p.id}:result:1"))
        assert message and message.card["can_confirm"]
        card = message.card
    ack = NotificationService(factory, clock=clock).display(
        sid,
        buyer,
        card["context_id"],
        "tab-a",
        uuid4().hex,
    )
    return card, ack


@pytest.mark.parametrize("before_interrupt", [False, True])
def test_approval_is_independent_and_confirmable_only_after_real_application(
    case, before_interrupt
):
    factory, sid, buyer, _, queue, runner = case
    p = pending(case)
    if not before_interrupt:
        assert p.runner.run(p.id)["result"] is None
    review(p)
    with factory() as db:
        assert db.get(Offer, p.offer).status.value == "PROPOSED"
        assert not db.get(NotificationDelivery, f"approval:{p.id}:result:1")
    # Waiting does not block a main-thread question or increase business version/rounds.
    query = queue.enqueue(sid, buyer, "query", TypedInput(kind="QUERY"))
    assert runner.run(query)
    p.runner.run(p.id)
    card, ack = approval_card(case, p)
    run = queue.enqueue(sid, buyer, "yes", confirm(card, ack))
    runner.run(run)
    assert receipt(factory, run).result["outcome"] == "INTENT_CONFIRMED"
    p.runner.run(p.id)
    with factory() as db:
        session = db.get(NegotiationSession, sid)
        assert session.status.value == "AGREED" and session.round_count == 1
        assert session.confirmation_source.value == "SELLER_APPROVED_BUYER_OFFER"
        assert db.get(ApprovalRequest, p.id).notification_status == "PUBLISHED"
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.source_id == f"approval:{p.id}:result:1")
            )
            == 1
        )


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("action", ["REPLACE", "WITHDRAW", "CLOSE"])
def test_a41_a51_old_decision_cannot_override_buyer_change(case, approved, action):
    factory, sid, buyer, _, queue, runner = case
    p = pending(case)
    p.runner.run(p.id)
    if approved:
        review(p)
    payload = (
        submit("810").model_copy(update={"kind": "REPLACE", "offer_id": p.offer})
        if action == "REPLACE"
        else TypedInput(kind=action, offer_id=p.offer if action == "WITHDRAW" else None)
    )
    run = queue.enqueue(sid, buyer, "change", payload)
    runner.run(run)
    p.runner.run(p.id)
    p.runner.run(p.id)
    assert p.commands.pending_events(p.id) == []
    with factory() as db:
        a, session = db.get(ApprovalRequest, p.id), db.get(NegotiationSession, sid)
        assert a.status.value == ("APPROVED" if approved else "CANCELLED")
        assert a.effect_status == "INVALIDATED" and a.invalidated_at and a.invalid_reason
        invalidation = db.get(BusinessActionReceipt, f"approval:{p.id}:invalidate:1")
        change_receipt = receipt(factory, run)
        assert (invalidation.before_version, invalidation.after_version) == (
            change_receipt.before_version,
            change_receipt.after_version,
        )
        assert db.get(Offer, p.offer).status.value == "WITHDRAWN"
        assert (
            session.status.value
            == {"REPLACE": "WAITING_APPROVAL", "WITHDRAW": "ACTIVE", "CLOSE": "CLOSED"}[action]
        )
        assert session.round_count == (2 if action == "REPLACE" else 1)
        message = db.scalar(select(Message).where(Message.source_id == f"approval:{p.id}:result:1"))
        assert not message.card["can_confirm"]
        if action == "REPLACE":
            assert a.superseded_by == session.current_offer_id


@pytest.mark.parametrize("action", ["WITHDRAW", "REPLACE", "CLOSE"])
def test_a42_accepted_offer_can_change_until_agreed(case, action):
    factory, sid, buyer, _, queue, runner = case
    p = pending(case)
    review(p)
    p.runner.run(p.id)
    card, ack = approval_card(case, p)
    payload = (
        submit("810")
        if action == "REPLACE"
        else TypedInput(
            kind=action,
            offer_id=p.offer if action == "WITHDRAW" else None,
        )
    )
    change = queue.enqueue(sid, buyer, "change", payload)
    stale = queue.enqueue(sid, buyer, "old-confirm", confirm(card, ack))
    runner.run(change)
    runner.run(stale)
    assert receipt(factory, stale) is None
    p.runner.run(p.id)
    with factory() as db:
        a = db.get(ApprovalRequest, p.id)
        assert a.status.value == "APPROVED" and a.effect_revision == 2
        assert a.effect_status == "INVALIDATED"
        assert db.get(Offer, p.offer).invalid_reason
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.source_id.like(f"approval:{p.id}:result:%"))
            )
            == 2
        )


def test_agreed_is_never_reopened_by_changes_or_expiry(case):
    factory, sid, buyer, clock, queue, runner = case
    p = pending(case)
    review(p)
    p.runner.run(p.id)
    card, ack = approval_card(case, p)
    run = queue.enqueue(sid, buyer, "confirm", confirm(card, ack))
    runner.run(run)
    for payload in [
        submit("810"),
        TypedInput(kind="WITHDRAW", offer_id=p.offer),
        TypedInput(kind="CLOSE"),
    ]:
        later = queue.enqueue(sid, buyer, uuid4().hex, payload)
        runner.run(later)
        assert receipt(factory, later) is None
    clock.advance(90000)
    p.commands.reconcile_offer(sid)
    p.runner.run(p.id)
    with factory() as db:
        assert db.get(NegotiationSession, sid).status.value == "AGREED"
        assert db.get(ApprovalRequest, p.id).effect_status == "APPLIED"


def test_a52_timely_decision_applies_after_response_deadline(case):
    factory, _, _, clock, _, _ = case
    p = pending(case)
    with factory() as db:
        deadline = db.get(ApprovalRequest, p.id).expires_at
        offer_expiry = db.get(Offer, p.offer).expires_at
    clock.now = deadline - timedelta(seconds=1)
    review(p)
    clock.advance(61)
    p.runner.run(p.id)
    approval_card(case, p)
    with factory() as db:
        assert db.get(Offer, p.offer).expires_at == offer_expiry
        assert db.get(ApprovalRequest, p.id).expires_at == deadline


def test_exact_response_deadline_expires_pending_and_rejects_late_seller(case):
    factory, sid, _, clock, _, _ = case
    p = pending(case)
    p.runner.run(p.id)
    with factory() as db:
        clock.now = db.get(ApprovalRequest, p.id).expires_at
    with pytest.raises(ApprovalConflictError):
        review(p)
    p.runner.run(p.id)
    with factory() as db:
        a = db.get(ApprovalRequest, p.id)
        assert a.status.value == "EXPIRED" and a.effect_status == "INVALIDATED"
        assert db.get(NegotiationSession, sid).status.value == "ACTIVE"
        assert db.get(Offer, p.offer).status.value == "WITHDRAWN"


def test_seller_rejection_effect_is_atomic_and_not_reapplied(case):
    factory, sid, buyer, _, queue, runner = case
    p = pending(case)
    review(p, False)
    with factory() as db:
        assert db.get(Offer, p.offer).status.value == "REJECTED"
        assert db.get(ApprovalRequest, p.id).effect_status == "APPLIED"
        assert db.get(NotificationDelivery, f"approval:{p.id}:result:1")
    new = queue.enqueue(sid, buyer, "new", submit("810"))
    runner.run(new)
    with factory() as db:
        version = db.get(NegotiationSession, sid).version
    p.runner.run(p.id)
    p.runner.run(p.id)
    with factory() as db:
        s = db.get(NegotiationSession, sid)
        assert s.status.value == "WAITING_APPROVAL" and s.version == version


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("point", ["decision", "effect", "receipt", "notification", "outbox"])
def test_seller_command_write_gaps_roll_back(case, approved, point):
    factory, _, _, clock, _, _ = case
    p = pending(case)

    def fail(at):
        if at == point:
            raise RuntimeError("write gap")

    p.commands = GraphApprovalService(factory, clock=clock, fault=fail)
    with pytest.raises(RuntimeError, match="write gap"):
        review(p, approved)
    with factory() as db:
        assert db.get(ApprovalRequest, p.id).status.value == "PENDING"
        assert db.get(Offer, p.offer).status.value == "PROPOSED"
        assert not db.get(OutboxEvent, f"approval:{p.id}:decision")
        assert not db.get(NotificationDelivery, f"approval:{p.id}:result:1")
    p.commands = GraphApprovalService(factory, clock=clock)
    review(p, approved)


@pytest.mark.parametrize("point", ["effect", "receipt", "notification", "outbox"])
def test_apply_write_gaps_roll_back_and_replay_once(case, point):
    factory, _, _, clock, _, _ = case
    p = pending(case)
    review(p)
    owner = p.commands.claim(p.id)

    def fail(at):
        if at == point:
            raise RuntimeError("write gap")

    failing = GraphApprovalService(factory, clock=clock, fault=fail)
    with pytest.raises(RuntimeError, match="write gap"):
        failing.apply(p.id, f"approval:{p.id}:decision", owner)
    with factory() as db:
        assert db.get(Offer, p.offer).status.value == "PROPOSED"
        assert db.get(ApprovalRequest, p.id).effect_revision == 0
        assert not db.get(NotificationDelivery, f"approval:{p.id}:result:1")
    original = p.commands.apply(p.id, f"approval:{p.id}:decision", owner)
    assert p.commands.apply(p.id, f"approval:{p.id}:decision", owner) == original


@pytest.mark.parametrize(
    "point", ["after_claim", "after_checkpoint", "after_apply", "after_publish", "after_end"]
)
def test_interrupt_and_failures_resume_in_another_process(case, point):
    factory, _, _, clock, _, _ = case
    p = pending(case)

    def process(fault=None):
        return subprocess.run(
            [sys.executable, "-m", "tests.graph.approval_process_worker"],
            input=json.dumps({"approval_id": p.id, "now": clock().isoformat(), "fault": fault}),
            capture_output=True,
            text=True,
            timeout=40,
        )

    # First process persists a real dynamic interrupt and exits normally.
    first = process()
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["result"] is None
    review(p)
    killed = process(point)
    assert killed.returncode == 73, killed.stderr
    clock.advance(31)
    recovered = process()
    assert recovered.returncode == 0, recovered.stderr
    approval_card(case, p)
    with factory() as db:
        assert db.get(ApprovalRequest, p.id).effect_revision == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.source_id == f"approval:{p.id}:result:1")
            )
            == 1
        )


def test_event_arrives_between_load_and_interrupt_is_redelivered(case):
    p = pending(case)

    def race(point):
        if point == "after_load":
            review(p)

    p.runner.fault = race
    assert p.runner.run(p.id)["result"] is None
    assert f"approval:{p.id}:decision" in p.commands.pending_events(p.id)
    p.runner.fault = lambda point: None
    p.runner.run(p.id)
    approval_card(case, p)


def test_wrong_event_or_stale_execution_cannot_apply(case):
    _, _, _, clock, _, _ = case
    p = pending(case)
    owner = p.commands.claim(p.id)
    with pytest.raises(Conflict):
        p.commands.apply(p.id, f"approval:{p.id}:requested", owner)
    review(p)
    clock.advance(31)
    newer = p.commands.claim(p.id)
    with pytest.raises(StaleOwner):
        p.commands.apply(p.id, f"approval:{p.id}:decision", owner)
    assert p.commands.apply(p.id, f"approval:{p.id}:decision", newer)


def test_lock_ownership_missing_checkpoint_and_independent_notice(case):
    factory, _, _, clock, _, runner = case
    p = pending(case)
    with (
        locked_saver(os.environ["P0_POSTGRES_URL"], approval_thread(p.id)),
        pytest.raises(ThreadBusy),
    ):
        p.runner.run(p.id)
    p.runner.run(p.id)
    review(p, False)
    with locked_saver(os.environ["P0_POSTGRES_URL"], approval_thread(p.id)) as (saver, _):
        saver.delete_thread(approval_thread(p.id))
    with pytest.raises(RecoveryConflict):
        p.runner.run(p.id)
    clock.advance(3)
    assert runner.notify(f"approval:{p.id}:result:1")
    with factory() as db:
        assert db.get(ApprovalExecution, p.id).recovery_required


def test_seller_idempotency_and_api_ownership(case):
    factory, _, buyer, _, _, _ = case
    p = pending(case)
    app = create_app()
    app.dependency_overrides[get_session_factory_dependency] = lambda: factory
    with TestClient(app) as client:
        path = f"/api/seller/approvals/{p.id}/approve"
        payload = {"request_id": "decision", "expected_effect_revision": 0, "comment": "ok"}
        assert client.post(path, json=payload).status_code == 401
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=buyer)
        assert client.post(path, json=payload).status_code == 404
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=p.seller)
        assert client.post(path, json={"request_id": "missing-version"}).status_code == 409
        first = client.post(path, json=payload)
        assert first.status_code == 200, first.text
        assert first.json()["effect_status"] == "NOT_APPLIED"
        assert client.post(path, json=payload).status_code == 200
        assert client.post(path, json={**payload, "comment": "different"}).status_code == 409
        assert client.post(path.replace("approve", "reject"), json=payload).status_code == 409


@pytest.mark.parametrize("mutation", ["offer_expiry", "policy", "product"])
def test_a53_approval_basis_rechecked_at_apply_and_confirmation(case, mutation):
    factory, sid, buyer, clock, queue, runner = case
    p = pending(case)
    review(p)
    p.runner.run(p.id)
    card, ack = approval_card(case, p)
    run = queue.enqueue(sid, buyer, "confirm", confirm(card, ack))
    with factory() as db, db.begin():
        if mutation == "offer_expiry":
            db.get(Offer, p.offer).expires_at = clock() - timedelta(seconds=1)
        else:
            pid = db.get(NegotiationSession, sid).product_id
            if mutation == "policy":
                db.scalar(select(SellerPolicy).where(SellerPolicy.product_id == pid)).version += 1
            else:
                db.get(Product, pid).status = "UNAVAILABLE"
    runner.run(run)
    assert receipt(factory, run) is None
    p.commands.reconcile_offer(sid)
    p.runner.run(p.id)
    with factory() as db:
        a = db.get(ApprovalRequest, p.id)
        assert a.status.value == "APPROVED" and a.effect_status == "INVALIDATED"


@pytest.mark.parametrize("mutation", ["cost_expiry", "cost_rule", "fact"])
def test_a53_semantic_approval_rechecks_cost_and_fact(semantic_case, mutation):  # noqa: F811
    c = semantic_case
    rule_id = rule(c)["rules"][0]["id"]
    fact(c, "battery_health_pct", "95")
    text = "电池至少90，800包邮到上海"
    run = turn(
        c,
        text,
        [
            intent(
                text,
                candidate=candidate(
                    "800",
                    "shipping",
                    region="上海",
                    conditions=[
                        {
                            "fact": "battery_health_pct",
                            "operator": ">=",
                            "value": "90",
                            "source_quote": "电池至少90",
                        }
                    ],
                ),
            )
        ],
    )
    approved = result(c, run)
    assert approved and approved["outcome"] == "WAITING_APPROVAL"
    service = GraphApprovalService(c.factory, clock=c.clock)
    service.review(
        seller_id=c.seller,
        approval_id=approved["approval_id"],
        request_id="approve",
        comment=None,
        approved=True,
        expected_revision=0,
    )
    if mutation == "fact":
        fact(c, "battery_health_pct", "85")
    elif mutation == "cost_rule":
        rule(c, cost="30", rule_id=rule_id)
    else:
        with c.factory() as db, db.begin():
            basis = db.get(SemanticOfferBasis, approved["offer_id"])
            db.get(CostQuote, basis.cost_quote_id).expires_at = c.clock()
    ApprovalRunner(c.factory, os.environ["P0_POSTGRES_URL"], clock=c.clock).run(
        approved["approval_id"]
    )
    with c.factory() as db:
        assert db.get(ApprovalRequest, approved["approval_id"]).effect_status == "INVALIDATED"
        assert db.get(Offer, approved["offer_id"]).status.value == "WITHDRAWN"


def test_agent_counter_is_rejected_on_replace_not_withdrawn(case):
    factory, sid, buyer, _, queue, runner = case
    first = queue.enqueue(sid, buyer, "counter", submit("700", counter_terms=Terms(amount="900")))
    runner.run(first)
    oid = receipt(factory, first).result["offer_id"]
    bad = queue.enqueue(sid, buyer, "withdraw-agent", TypedInput(kind="WITHDRAW", offer_id=oid))
    runner.run(bad)
    assert receipt(factory, bad) is None
    newer = queue.enqueue(sid, buyer, "new", submit("800"))
    runner.run(newer)
    with factory() as db:
        assert db.get(Offer, oid).status.value == "REJECTED"


def test_parallel_apply_and_replace_have_one_serializable_outcome(case):
    factory, sid, buyer, _, queue, runner = case
    p = pending(case)
    review(p)
    change = queue.enqueue(sid, buyer, "replace", submit("810"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(p.runner.run, p.id), pool.submit(runner.run, change)]
        for future in futures:
            future.result(timeout=20)
    # A stale preauthorization is allowed to decline the input; retry a fresh proposal.
    if not receipt(factory, change):
        change = queue.enqueue(sid, buyer, "replace-again", submit("810"))
        runner.run(change)
    p.runner.run(p.id)
    with factory() as db:
        session = db.get(NegotiationSession, sid)
        assert session.status.value == "WAITING_APPROVAL"
        assert session.current_offer_id != p.offer
        assert db.get(ApprovalRequest, p.id).effect_status == "INVALIDATED"


def test_worker_reconciles_timeout_and_dispatches_events_without_manual_run(case):
    factory, sid, _, _, _, _ = case
    p = pending(case)
    run_batch(factory, os.environ["P0_POSTGRES_URL"], limit=200)
    with factory() as db, db.begin():
        a = db.get(ApprovalRequest, p.id)
        a.expires_at -= timedelta(hours=1)
    # The session fixture also contains deliberately damaged recovery cases; assert our object.
    run_batch(factory, os.environ["P0_POSTGRES_URL"], limit=200)
    with factory() as db:
        assert db.get(ApprovalRequest, p.id).status.value == "EXPIRED"
        assert db.get(ApprovalRequest, p.id).notification_status == "PUBLISHED"
        assert db.get(NegotiationSession, sid).status.value == "ACTIVE"
    assert p.commands.pending_events(p.id) == []


@pytest.mark.parametrize("counter", [False, True])
def test_unapproved_offer_expiry_has_unique_receipt_and_notice(case, counter):
    factory, sid, buyer, clock, queue, runner = case
    first = queue.enqueue(
        sid,
        buyer,
        "offer",
        submit("700", counter_terms=Terms(amount="900")) if counter else submit(),
    )
    runner.run(first)
    oid = receipt(factory, first).result["offer_id"]
    clock.advance(86401)
    service = GraphApprovalService(factory, clock=clock)
    service.reconcile_offer(sid)
    service.reconcile_offer(sid)
    assert runner.notify(f"offer:{oid}:invalidated")
    with factory() as db:
        assert db.get(Offer, oid).status.value == ("REJECTED" if counter else "WITHDRAWN")
        assert db.get(NegotiationSession, sid).current_offer_id is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.source_id == f"offer:{oid}:invalidated")
            )
            == 1
        )


def test_same_request_id_from_different_sellers_does_not_collide(case, graph_factory):
    from app.runtime.graph_runner import GraphRunner
    from app.runtime.input_queue import InputQueue
    from tests.graph.conftest import Clock
    from tests.integration.factories import create_negotiation

    p = pending(case)
    review(p, request_id="same-key")
    sid, buyer = create_negotiation(graph_factory)
    clock = Clock()
    queue = InputQueue(graph_factory, clock=clock)
    queue.enroll(sid, buyer)
    with graph_factory() as db:
        pid = db.get(NegotiationSession, sid).product_id
        policy = db.scalar(select(SellerPolicy).where(SellerPolicy.product_id == pid))
        amount = str(policy.minimum_net_price)
    second = pending(
        (
            graph_factory,
            sid,
            buyer,
            clock,
            queue,
            GraphRunner(graph_factory, os.environ["P0_POSTGRES_URL"], queue=queue),
        ),
        amount,
    )
    assert second.seller != p.seller
    review(second, request_id="same-key")


def test_old_accept_notice_published_after_invalidation_has_no_card_authority(case):
    factory, sid, buyer, clock, queue, runner = case
    p = pending(case)
    review(p)

    def fail(point):
        if point == "after_apply":
            raise RuntimeError("stop before publication")

    p.runner.fault = fail
    with pytest.raises(RuntimeError):
        p.runner.run(p.id)
    change = queue.enqueue(sid, buyer, "replace", submit("810"))
    runner.run(change)
    # Deliver revision 2 before the old acceptance revision 1.
    newer = runner.notify(f"approval:{p.id}:result:2")
    older = runner.notify(f"approval:{p.id}:result:1")
    with factory() as db:
        assert db.get(Message, newer) and "历史结果" in db.get(Message, older).content
        assert not db.get(Message, older).card["can_confirm"]
        assert not db.scalar(
            select(PublicInteractionContext).where(PublicInteractionContext.message_id == older)
        )
        assert db.get(ApprovalRequest, p.id).notification_status == "PUBLISHED"
        assert db.get(NegotiationSession, sid).status.value == "WAITING_APPROVAL"
    clock.advance(3)
    p.runner.fault = lambda point: None
    p.runner.run(p.id)
    p.runner.run(p.id)
    assert p.commands.pending_events(p.id) == []


def test_semantic_progress_rechecks_state_at_publish_and_cost_expiry_bounds_card(semantic_case):  # noqa: F811
    c = semantic_case
    rule(c)
    original = result(
        c,
        turn(
            c,
            "800包邮到上海",
            [intent("800包邮到上海", candidate=candidate("800", "shipping", region="上海"))],
        ),
    )
    aid, oid = original["approval_id"], original["offer_id"]
    service = GraphApprovalService(c.factory, clock=c.clock)
    with c.factory() as db, db.begin():
        quote = db.get(CostQuote, db.get(SemanticOfferBasis, oid).cost_quote_id)
        quote.expires_at = c.clock() + timedelta(minutes=10)
        expiry = quote.expires_at
    question = "卖家审批通过了吗？"
    changed = False

    def approve_during_turn(point):
        nonlocal changed
        if point == "after_plan" and not changed:
            changed = True
            service.review(
                seller_id=c.seller,
                approval_id=aid,
                request_id=uuid4().hex,
                approved=True,
                comment=None,
                expected_revision=0,
            )

    c.runner.fault = approve_during_turn
    run = turn(c, question, [intent(question, kind="ASK_TERMS", topic="approval_status")])
    with c.factory() as db:
        reply = db.scalar(select(Message).where(Message.source_id == f"turn:{run}:reply"))
        assert "等待系统复核" in reply.content
    ApprovalRunner(c.factory, os.environ["P0_POSTGRES_URL"], clock=c.clock).run(aid)
    with c.factory() as db:
        context = db.scalar(
            select(PublicInteractionContext).where(PublicInteractionContext.offer_id == oid)
        )
        assert context.expires_at == expiry


def test_different_approval_event_cannot_authorize_current_approval(case):
    p = pending(case)
    review(p)
    second = pending(case, "810")
    owner = second.commands.claim(second.id)
    with pytest.raises(Conflict, match="事件"):
        second.commands.apply(second.id, f"approval:{p.id}:decision", owner)
