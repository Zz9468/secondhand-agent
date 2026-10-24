from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.models import ApprovalRequest, Message, NegotiationSession, Offer, SellerPolicy
from app.db.models.graph import BusinessActionReceipt, NotificationDelivery, OutboxEvent
from app.runtime.contracts import Conflict, Terms, TypedInput
from app.services.business_command_service import BusinessCommandService
from app.services.graph_status_service import GraphStatusService


def submit(amount="900", **kwargs):
    return TypedInput(kind="SUBMIT", terms=Terms(amount=amount), **kwargs)


def receipt(factory, run_id):
    with factory() as db:
        return db.get(BusinessActionReceipt, f"turn:{run_id}:action")


@pytest.mark.parametrize(
    "amount,outcome",
    [("850", "OFFER_ACCEPTED"), ("750", "WAITING_APPROVAL"), ("749.99", "OFFER_REJECTED")],
)
def test_three_price_zones_use_real_orm(case, amount, outcome):
    factory, sid, buyer, _, queue, runner = case
    run_id = queue.enqueue(sid, buyer, "one", submit(amount))
    assert runner.run(run_id)
    status = GraphStatusService(factory).get(sid, buyer, run_id)
    assert status["business_status"] == "COMMITTED"
    assert status["business_outcome"] == outcome
    assert status["notification_status"] == "PUBLISHED"
    with factory() as db:
        session = db.get(NegotiationSession, sid)
        assert session.round_count == session.last_resolved_input_seq == 1
        assert (
            db.scalar(select(func.count()).select_from(Offer).where(Offer.session_id == sid)) == 1
        )
        message = db.get(Message, status["public_message_id"])
        assert message.card["can_confirm"] == (outcome == "OFFER_ACCEPTED")
        assert db.scalar(
            select(func.count())
            .select_from(ApprovalRequest)
            .where(ApprovalRequest.session_id == sid)
        ) == int(outcome == "WAITING_APPROVAL")


@pytest.mark.parametrize(
    "point", ["offer", "resolution", "round", "receipt", "notification", "outbox"]
)
def test_a31_atomic_rollback_and_replay(case, point):
    factory, sid, buyer, _, queue, _ = case
    run_id = queue.enqueue(sid, buyer, "one", submit("800"))
    owner = queue.claim(run_id)

    def fail(at):
        if at == point:
            raise RuntimeError("injected write gap")

    with pytest.raises(RuntimeError, match="write gap"):
        BusinessCommandService(factory, clock=queue.clock, fault=fail).execute(run_id, owner, 1)
    with factory() as db:
        assert db.get(NegotiationSession, sid).round_count == 0
        assert db.get(NegotiationSession, sid).version == 1
        assert not db.scalar(select(Offer).where(Offer.session_id == sid))
        assert not db.scalar(select(ApprovalRequest).where(ApprovalRequest.session_id == sid))
        assert not db.get(BusinessActionReceipt, f"turn:{run_id}:action")
        assert not db.get(NotificationDelivery, f"turn:{run_id}:reply")
        assert not db.get(OutboxEvent, f"turn:{run_id}:reply")
    service = BusinessCommandService(factory, clock=queue.clock)
    original = service.execute(run_id, owner, 1)
    assert service.execute(run_id, owner, 1) == original
    with pytest.raises(Conflict, match="载荷冲突"):
        service.execute(run_id, owner, 2)
    with factory() as db:
        assert db.get(NegotiationSession, sid).round_count == 1


@pytest.mark.parametrize(
    "terms",
    [
        Terms(amount="900", shipping_paid_by="seller"),
        Terms(amount="900", seller_discount=None),
        Terms(amount="900", additional_terms={"warranty": "forever"}),
    ],
)
def test_unknown_cost_or_unsupported_terms_never_commit(case, terms):
    factory, sid, buyer, _, queue, runner = case
    run_id = queue.enqueue(sid, buyer, "invalid", TypedInput(kind="SUBMIT", terms=terms))
    runner.run(run_id)
    assert receipt(factory, run_id) is None
    with factory() as db:
        assert db.get(NegotiationSession, sid).round_count == 0


@pytest.mark.parametrize(
    "amount,counter,outcome",
    [
        ("900", None, "OFFER_ACCEPTED"),
        ("800", None, "WAITING_APPROVAL"),
        ("700", "875", "COUNTER_OFFERED"),
        ("700", None, "OFFER_REJECTED"),
    ],
)
def test_a43_last_round_resolves_and_rejects_next_proposal(case, amount, counter, outcome):
    factory, sid, buyer, _, queue, runner = case
    with factory() as db, db.begin():
        session = db.get(NegotiationSession, sid)
        policy = db.scalar(
            select(SellerPolicy).where(SellerPolicy.product_id == session.product_id)
        )
        policy.max_rounds = 1
    run = queue.enqueue(
        sid, buyer, "last", submit(amount, counter_terms=Terms(amount=counter) if counter else None)
    )
    runner.run(run)
    assert receipt(factory, run).result["outcome"] == outcome
    next_run = queue.enqueue(sid, buyer, "exceeds", submit("950"))
    runner.run(next_run)
    assert receipt(factory, next_run) is None
    with factory() as db:
        assert db.get(NegotiationSession, sid).round_count == 1


def test_a44_equivalent_proposal_no_new_round_and_query_no_revision(case):
    factory, sid, buyer, _, queue, runner = case
    first = queue.enqueue(sid, buyer, "one", submit())
    runner.run(first)
    second = queue.enqueue(sid, buyer, "two", submit())
    runner.run(second)
    assert receipt(factory, second).result["reused"]
    assert receipt(factory, first).result["offer_id"] == receipt(factory, second).result["offer_id"]
    query = queue.enqueue(sid, buyer, "query", TypedInput(kind="QUERY"))
    runner.run(query)
    with factory() as db:
        assert db.get(NegotiationSession, sid).round_count == 1
        assert db.get(NegotiationSession, sid).version == 2
    assert receipt(factory, query) is None


def test_rejected_new_proposal_preserves_previous_approval(case):
    factory, sid, buyer, _, queue, runner = case
    first = queue.enqueue(sid, buyer, "one", submit("800"))
    runner.run(first)
    old = receipt(factory, first).result
    second = queue.enqueue(sid, buyer, "two", submit("700"))
    runner.run(second)
    with factory() as db:
        assert db.get(NegotiationSession, sid).current_offer_id == old["offer_id"]
        assert db.get(ApprovalRequest, old["approval_id"]).status.value == "PENDING"


def test_counter_candidate_is_authorized_itself(case):
    factory, sid, buyer, _, queue, runner = case
    run = queue.enqueue(
        sid,
        buyer,
        "one",
        submit(
            "700",
            counter_terms=Terms(
                amount="860", shipping_paid_by="seller", shipping_cost=Decimal("20")
            ),
        ),
    )
    runner.run(run)
    assert receipt(factory, run) is None
