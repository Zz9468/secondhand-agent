from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.db.models import Message, ModelExecutionTask, NegotiationSession, Offer, SellerPolicy
from app.db.models.graph import NegotiationInput
from app.runtime.contracts import Conflict, Terms, TypedInput
from app.services.graph_status_service import GraphStatusService
from app.services.notification_service import NotificationService
from tests.graph.test_business import receipt, submit


def shown(case, *, amount="900", view="tab-a"):
    factory, sid, buyer, _, queue, runner = case
    run_id = queue.enqueue(sid, buyer, uuid4().hex, submit(amount))
    result = runner.run(run_id)
    with factory() as db:
        card = db.get(Message, result["public_message_id"]).card
    ack = NotificationService(factory, clock=queue.clock).display(
        sid,
        buyer,
        card["context_id"],
        view,
        uuid4().hex,
    )
    return run_id, card, ack


def confirm(card, ack, view="tab-a"):
    return TypedInput(
        kind="CONFIRM",
        offer_id=card["offer_id"],
        display_receipt_id=ack,
        client_view_id=view,
        text="行",
    )


def test_confirm_after_display_records_intent_once(case):
    factory, sid, buyer, _, queue, runner = case
    _, card, ack = shown(case)
    run_id = queue.enqueue(sid, buyer, "confirm", confirm(card, ack))
    runner.run(run_id)
    assert runner.run(run_id)
    assert receipt(factory, run_id).result["outcome"] == "INTENT_CONFIRMED"
    with factory() as db:
        session = db.get(NegotiationSession, sid)
        assert session.status.value == "AGREED"
        assert session.round_count == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.source_id == f"turn:{run_id}:reply")
            )
            == 1
        )


def test_a25_later_display_cannot_authorize_already_queued_confirmation(case):
    factory, sid, buyer, clock, queue, runner = case

    def fail(point):
        if point == "after_command":
            raise RuntimeError("after command")

    first = queue.enqueue(sid, buyer, "first", submit())
    runner.fault = fail
    with pytest.raises(RuntimeError):
        runner.run(first)
    offer = receipt(factory, first).result["offer_id"]
    second = queue.enqueue(sid, buyer, "early-yes", TypedInput(kind="CONFIRM", offer_id=offer))
    assert runner.run(second) is None
    clock.advance()
    runner.fault = lambda point: None
    result = runner.run(first)
    with factory() as db:
        card = db.get(Message, result["public_message_id"]).card
    ack = NotificationService(factory, clock=clock).display(
        sid, buyer, card["context_id"], "tab-a", "display"
    )
    assert ack
    runner.run(second)
    assert receipt(factory, second) is None
    with factory() as db:
        assert db.get(NegotiationSession, sid).status.value == "ACTIVE"
        assert db.get(NegotiationInput, second).payload["display_receipt_id"] is None


def test_a26_a28_old_view_confirmation_queued_after_replace_is_not_rebound(case):
    factory, sid, buyer, _, queue, runner = case
    _, old, old_ack = shown(case)
    replace = queue.enqueue(
        sid,
        buyer,
        "replace",
        TypedInput(kind="REPLACE", offer_id=old["offer_id"], terms=Terms(amount="950")),
    )
    stale = queue.enqueue(sid, buyer, "old-yes", confirm(old, old_ack))
    close = queue.enqueue(sid, buyer, "close", TypedInput(kind="CLOSE"))
    assert runner.run(stale) is None
    assert runner.run(close) is None
    runner.run(replace)
    runner.run(stale)
    assert receipt(factory, stale) is None
    runner.run(close)
    with factory() as db:
        session = db.get(NegotiationSession, sid)
        assert session.status.value == "CLOSED"
        assert session.round_count == 2
        assert db.get(Offer, old["offer_id"]).status.value == "WITHDRAWN"


def test_a27_retry_wait_and_failed_labels_keep_fifo_barrier(case):
    factory, sid, buyer, clock, queue, runner = case
    first = queue.enqueue(sid, buyer, "first", submit())
    second = queue.enqueue(sid, buyer, "second", TypedInput(kind="CLOSE"))
    owner = queue.claim(first)
    queue.release(first, owner)
    assert runner.run(second) is None
    assert GraphStatusService(factory).get(sid, buyer, second)["blocked_by_input_seq"] == 1
    with factory() as db, db.begin():
        task = db.get(ModelExecutionTask, queue.read(first)["task_id"])
        task.status = "FAILED"
        task.next_retry_at = None
    assert runner.run(second) is None
    with factory() as db, db.begin():
        db.get(ModelExecutionTask, queue.read(first)["task_id"]).status = "PENDING"
    clock.advance()
    runner.run(first)
    runner.run(second)


def test_a45_request_fingerprint_includes_type_view_receipt(case):
    _, sid, buyer, _, queue, _ = case
    _, card, ack = shown(case)
    payload = confirm(card, ack)
    first = queue.enqueue(sid, buyer, "same", payload)
    assert first == queue.enqueue(sid, buyer, "same", payload)
    for changed in (
        payload.model_copy(update={"display_receipt_id": None}),
        payload.model_copy(update={"client_view_id": "other-tab"}),
        TypedInput(kind="CLOSE"),
    ):
        with pytest.raises(Conflict, match="载荷不一致"):
            queue.enqueue(sid, buyer, "same", changed)


def test_receipt_ownership_view_and_expiration_are_checked(case):
    _, sid, buyer, clock, queue, _ = case
    _, card, ack = shown(case)
    with pytest.raises(Conflict):
        queue.enqueue(sid, buyer, "bad-view", confirm(card, ack, "tab-b"))
    with pytest.raises(Conflict):
        queue.enqueue(sid, "someone-else", "bad-actor", confirm(card, ack))
    clock.advance(1801)
    with pytest.raises(Conflict):
        queue.enqueue(sid, buyer, "expired", confirm(card, ack))


@pytest.mark.parametrize("change", ["expiry", "policy", "product"])
def test_confirm_revalidates_after_enqueue(case, change):
    factory, sid, buyer, clock, queue, runner = case
    _, card, ack = shown(case)
    run = queue.enqueue(sid, buyer, "yes", confirm(card, ack))
    if change == "expiry":
        clock.advance(1801)
    else:
        from app.db.models import Product

        with factory() as db, db.begin():
            session = db.get(NegotiationSession, sid)
            if change == "policy":
                db.scalar(
                    select(SellerPolicy).where(SellerPolicy.product_id == session.product_id)
                ).version += 1
            else:
                db.get(Product, session.product_id).description = "updated facts"
    runner.run(run)
    assert receipt(factory, run) is None


def test_queue_is_capped_and_duplicate_requests_do_not_consume_slots(case):
    factory, sid, buyer, _, queue, _ = case
    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(lambda _: queue.enqueue(sid, buyer, "same", submit()), range(8)))
    assert len(set(runs)) == 1
    for i in range(19):
        queue.enqueue(sid, buyer, str(i), TypedInput(kind="QUERY"))
    assert queue.enqueue(sid, buyer, "same", submit()) == runs[0]
    with pytest.raises(Conflict, match="20"):
        queue.enqueue(sid, buyer, "full", submit())
    with factory() as db:
        assert db.get(NegotiationSession, sid).next_input_seq == 21


def test_a43_final_round_can_be_confirmed(case):
    factory, sid, buyer, _, queue, runner = case
    with factory() as db, db.begin():
        session = db.get(NegotiationSession, sid)
        db.scalar(
            select(SellerPolicy).where(SellerPolicy.product_id == session.product_id)
        ).max_rounds = 1
    _, card, ack = shown(case)
    run = queue.enqueue(sid, buyer, "yes", confirm(card, ack))
    runner.run(run)
    assert receipt(factory, run).result["outcome"] == "INTENT_CONFIRMED"
