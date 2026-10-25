import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select

from app.db.base import Base
from app.db.models import NegotiationSession, Product, SellerPolicy
from app.runtime.contracts import TypedInput
from app.services.approval_service import ApprovalService
from app.services.errors import NegotiationLifecycleConflictError
from tests.graph.test_business import receipt, submit


def test_migration_matches_orm_after_upgrade_downgrade_upgrade(graph_factory):
    with graph_factory.kw["bind"].connect() as connection:
        from app.p0.store import metadata as probe_metadata

        # P0 and graph tests intentionally share an isolated DB. Only the registered
        # probe tables are out of scope; unknown application tables must still fail.
        context = MigrationContext.configure(
            connection,
            opts={
                "include_object": lambda obj, name, kind, reflected, compare_to: (
                    kind != "table" or name not in probe_metadata.tables
                )
            },
        )
        assert compare_metadata(context, Base.metadata) == []


def test_wrong_replacement_target_does_not_reuse_identical_current_terms(case):
    factory, sid, buyer, _, queue, runner = case
    first = queue.enqueue(sid, buyer, "first", submit())
    runner.run(first)
    changed = submit().model_copy(update={"kind": "REPLACE", "offer_id": 99999999})
    second = queue.enqueue(sid, buyer, "wrong-object", changed)
    runner.run(second)
    assert receipt(factory, second) is None
    with factory() as db:
        assert db.get(NegotiationSession, sid).round_count == 1


def test_inputs_queued_after_close_receive_results_without_business_effect(case):
    factory, sid, buyer, _, queue, runner = case
    close = queue.enqueue(sid, buyer, "close", TypedInput(kind="CLOSE"))
    later = queue.enqueue(sid, buyer, "offer", submit())
    runner.run(close)
    runner.run(later)
    assert receipt(factory, later) is None
    with factory() as db:
        session = db.get(NegotiationSession, sid)
        assert session.last_resolved_input_seq == 2
        assert session.round_count == 0


def test_legacy_approval_and_expiry_do_not_process_graph_sessions(case):
    factory, sid, buyer, clock, queue, runner = case
    run = queue.enqueue(sid, buyer, "one", submit("800"))
    runner.run(run)
    approval_id = receipt(factory, run).result["approval_id"]
    clock.advance(1801)
    service = ApprovalService(factory)
    assert service.expire_due_requests(now=clock()) == ()
    with pytest.raises(NegotiationLifecycleConflictError):
        service.expire_request(approval_id=approval_id, now=clock())


def test_confirmation_rechecks_private_authorization_even_with_unchanged_policy_version(case):
    from tests.graph.test_display_queue import confirm, shown

    factory, sid, buyer, _, queue, runner = case
    _, card, ack = shown(case)
    run = queue.enqueue(sid, buyer, "yes", confirm(card, ack))
    with factory() as db, db.begin():
        session = db.get(NegotiationSession, sid)
        policy = db.scalar(
            select(SellerPolicy).where(SellerPolicy.product_id == session.product_id)
        )
        policy.auto_accept_threshold = 999
    runner.run(run)
    assert receipt(factory, run) is None


def test_product_unavailable_between_input_and_execution_prevents_commit(case):
    factory, sid, buyer, _, queue, runner = case
    run = queue.enqueue(sid, buyer, "one", submit())
    with factory() as db, db.begin():
        session = db.get(NegotiationSession, sid)
        db.get(Product, session.product_id).status = "UNAVAILABLE"
    runner.run(run)
    assert receipt(factory, run) is None
