"""Real InnoDB regression for unrelated approvals competing on absent notification keys."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import AgentSessionContext, ApprovalRequest, NegotiationSession
from app.db.models.graph import NotificationDelivery, OutboxEvent
from app.runtime.contracts import Terms, TypedInput, utcnow
from app.runtime.input_queue import InputQueue
from app.services.agent_message_service import AgentMessageService
from app.services.approval_lifecycle import effect_notice, event_in_transaction
from app.services.business_command_service import BusinessCommandService
from tests.integration.factories import create_negotiation


@pytest.mark.parametrize("kind", ["event", "notice", "context"])
def test_distinct_approvals_do_not_deadlock_on_absent_keys(mysql_engine, kind):
    # This test commits from multiple connections; never permit a live application database.
    assert mysql_engine.url.database.startswith("v4_suite_check_")
    engine = mysql_engine.execution_options(isolation_level="REPEATABLE READ")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    approvals = []
    for _ in range(2):
        sid, buyer = create_negotiation(factory)
        queue = InputQueue(factory)
        queue.enroll(sid, buyer)
        run = queue.enqueue(
            sid, buyer, uuid4().hex, TypedInput(kind="SUBMIT", terms=Terms(amount="2800"))
        )
        result = BusinessCommandService(factory).execute(run, queue.claim(run), 1)
        approvals.append((sid, result["approval_id"]))
    keys = {
        f"approval:{aid}:requested" if kind == "event" else f"approval:{aid}:result:0"
        for _, aid in approvals
    }
    if kind == "context":
        keys = {sid for sid, _ in approvals}
    if kind == "event":
        with factory() as db, db.begin():
            db.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(keys)))
    # Place both missing keys in one known primary-key gap, independent of other test fixtures.
    target = OutboxEvent if kind == "event" else NotificationDelivery
    if kind == "context":
        target = AgentSessionContext
    gap_barrier, start = Barrier(2), Barrier(2)

    class InterleavedSession(Session):
        def get(self, entity, ident, **kwargs):
            result = super().get(entity, ident, **kwargs)
            if (
                entity is target
                and ident in keys
                and result is None
                and kwargs.get("with_for_update")
            ):
                gap_barrier.wait(timeout=10)
            return result

    concurrent = sessionmaker(bind=engine, class_=InterleavedSession, expire_on_commit=False)

    def write(pair):
        sid, aid = pair
        try:
            with concurrent() as db, db.begin():
                session = db.get(NegotiationSession, sid, with_for_update=True)
                approval = db.get(ApprovalRequest, aid, with_for_update=True)
                start.wait(timeout=10)
                if kind == "event":
                    event_in_transaction(db, approval, "requested", "REQUESTED")
                elif kind == "context":
                    AgentMessageService.context(db, sid)
                else:
                    effect_notice(db, session, approval, "APPROVAL_INVALIDATED", utcnow())
            return "OK"
        except OperationalError as exc:
            return exc.orig.args[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, approvals))
    assert results == ["OK", "OK"], results
    if kind == "context":
        with factory() as db, db.begin():
            for key in keys:
                row = db.get(AgentSessionContext, key)
                row.next_stream_seq, row.summary_text = 9, "preserved-summary"
        with factory() as db, db.begin():
            for key in keys:
                row = AgentMessageService.context(db, key)
                assert row.next_stream_seq == 9 and row.summary_text == "preserved-summary"
        return
    # Idempotent re-emission must not reset already published work to pending.
    with factory() as db, db.begin():
        for key in keys:
            db.get(OutboxEvent, key).status = "DELIVERED"
            if kind == "notice":
                db.get(NotificationDelivery, key).status = "PUBLISHED"
    for sid, aid in approvals:
        with factory() as db, db.begin():
            session = db.get(NegotiationSession, sid, with_for_update=True)
            approval = db.get(ApprovalRequest, aid, with_for_update=True)
            if kind == "event":
                event_in_transaction(db, approval, "requested", "REQUESTED")
            else:
                effect_notice(db, session, approval, "APPROVAL_INVALIDATED", utcnow())
    with factory() as db:
        assert all(db.get(OutboxEvent, key).status == "DELIVERED" for key in keys)
        if kind == "notice":
            assert all(db.get(NotificationDelivery, key).status == "PUBLISHED" for key in keys)
