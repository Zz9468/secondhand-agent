import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app.db.models import Message, ModelExecutionTask, NegotiationSession, Offer
from app.db.models.graph import PublicInteractionContext
from app.graphs.negotiation.graph import build_graph
from app.persistence.checkpointer import ThreadBusy, locked_saver
from app.runtime.contracts import CONFIG, TypedInput, thread_id
from app.runtime.recovery import RecoveryConflict
from app.services.graph_status_service import GraphStatusService
from tests.graph.test_business import receipt, submit


@pytest.mark.parametrize("point", ["after_claim", "after_command", "after_publish", "after_end"])
def test_a30_a60_real_process_exit_and_restart(case, point):
    factory, sid, buyer, clock, queue, runner = case
    run = queue.enqueue(sid, buyer, "one", submit())

    def process(fault=""):
        return subprocess.run(
            [sys.executable, "-m", "tests.graph.process_worker"],
            input=json.dumps({"run_id": run, "now": clock().isoformat(), "fault": fault}),
            capture_output=True,
            text=True,
            timeout=30,
        )

    killed = process(point)
    assert killed.returncode == 73, killed.stderr
    with factory() as db:
        assert db.get(NegotiationSession, sid).last_resolved_input_seq == 0
    clock.advance(31)
    restored = process()
    assert restored.returncode == 0, restored.stderr
    original = json.loads(restored.stdout)
    next_run = queue.enqueue(sid, buyer, "two", TypedInput(kind="QUERY"))
    runner.run(next_run)
    assert runner.run(run) == original  # A29：历史任务只回原结果。
    with factory() as db:
        session = db.get(NegotiationSession, sid)
        assert session.round_count == 1
        assert session.last_resolved_input_seq == 2
        assert (
            db.scalar(select(func.count()).select_from(Offer).where(Offer.session_id == sid)) == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.source_id == f"turn:{run}:reply")
            )
            == 1
        )
    with locked_saver(os.environ["P0_POSTGRES_URL"], thread_id(sid)) as (saver, _):
        values = saver.get_tuple({"configurable": {"thread_id": thread_id(sid)}}).checkpoint[
            "channel_values"
        ]
        assert values["run_id"] == next_run
        assert values["business_result"]["outcome"] == "PRODUCT_INFO"
        assert values["authorization"] == "NO_PRICE_ACTION"
        assert values["stop_reason"] is None


def test_a34_a35_independent_notification_without_model_or_checkpoint_execution(case):
    factory, sid, buyer, clock, queue, runner = case
    run = queue.enqueue(
        sid, buyer, "one", submit(), config={**CONFIG, "max_calls": 0, "max_tokens": 0}
    )

    def fail(point):
        if point == "after_command":
            raise RuntimeError("checkpoint failure")

    runner.fault = fail
    with pytest.raises(RuntimeError):
        runner.run(run)
    status = GraphStatusService(factory).get(sid, buyer, run)
    assert status["business_status"] == "COMMITTED"
    assert status["notification_status"] == "PENDING"
    source = f"turn:{run}:reply"
    clock.advance()
    message = runner.notify(source)
    assert message
    with factory() as db:
        assert db.get(NegotiationSession, sid).last_resolved_input_seq == 0
    runner.fault = lambda point: None
    assert runner.run(run)["public_message_id"] == message
    with factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Message).where(Message.source_id == source))
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(PublicInteractionContext)
                .where(PublicInteractionContext.message_id == message)
            )
            == 1
        )


@pytest.mark.parametrize("damage", ["missing", "version"])
def test_a46_evidence_without_matching_checkpoint_keeps_barrier(case, damage):
    factory, sid, buyer, clock, queue, runner = case
    run = queue.enqueue(sid, buyer, "one", submit())
    later = queue.enqueue(sid, buyer, "two", TypedInput(kind="CLOSE"))

    def fail(point):
        if point == "after_command":
            raise RuntimeError("crash")

    runner.fault = fail
    with pytest.raises(RuntimeError):
        runner.run(run)
    with locked_saver(os.environ["P0_POSTGRES_URL"], thread_id(sid)) as (saver, _):
        if damage == "missing":
            saver.delete_thread(thread_id(sid))
        else:
            graph = build_graph(saver, queue, ("not-used", 0))
            graph.update_state(
                {"configurable": {"thread_id": thread_id(sid)}}, {"graph_version": "wrong"}
            )
    clock.advance()
    runner.fault = lambda point: None
    with pytest.raises(RecoveryConflict):
        runner.run(run)
    assert queue.read(run)["recovery_required"]
    assert runner.run(later) is None
    # 独立通知不依赖图 checkpoint，但也不解除屏障。
    assert runner.notify(f"turn:{run}:reply")
    with factory() as db:
        assert db.get(NegotiationSession, sid).last_resolved_input_seq == 0


def test_thread_lock_precedes_lease_and_parallel_publish_is_unique(case):
    factory, sid, buyer, clock, queue, runner = case
    run = queue.enqueue(sid, buyer, "one", submit())
    with locked_saver(os.environ["P0_POSTGRES_URL"], thread_id(sid)), pytest.raises(ThreadBusy):
        runner.run(run)
    with factory() as db:
        assert db.get(ModelExecutionTask, queue.read(run)["task_id"]).attempt_count == 0

    def fail(point):
        if point == "after_command":
            raise RuntimeError("crash")

    runner.fault = fail
    with pytest.raises(RuntimeError):
        runner.run(run)
    clock.advance()
    runner.fault = lambda point: None

    def normal_or_notify(which):
        try:
            return runner.run(run) if which else runner.notify(f"turn:{run}:reply")
        except ThreadBusy:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(normal_or_notify, [True, False]))
    clock.advance()
    runner.run(run)
    with factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.source_id == f"turn:{run}:reply")
            )
            == 1
        )


def test_a36_superseded_committed_result_has_no_confirmation_card(case):
    factory, sid, buyer, clock, queue, runner = case
    run = queue.enqueue(sid, buyer, "one", submit())

    def fail(point):
        if point == "after_command":
            raise RuntimeError("crash")

    runner.fault = fail
    with pytest.raises(RuntimeError):
        runner.run(run)
    # 模拟独立业务事件已在会话锁下改变当前对象（P3 将提供事件入口）。
    with factory() as db, db.begin():
        session = db.get(NegotiationSession, sid, with_for_update=True)
        db.get(Offer, session.current_offer_id).status = "WITHDRAWN"
        session.current_offer_id = None
        session.version += 1
    clock.advance()
    message_id = runner.notify(f"turn:{run}:reply")
    assert receipt(factory, run).result["outcome"] == "OFFER_ACCEPTED"
    with factory() as db:
        message = db.get(Message, message_id)
        assert "历史结果" in message.content
        assert not message.card["can_confirm"]
        assert not message.card["context_id"]


def test_notification_retries_remain_visible_and_do_not_erase_receipt(case):
    from app.services.notification_service import NotificationService

    factory, sid, buyer, clock, queue, runner = case
    run = queue.enqueue(sid, buyer, "one", submit())
    owner = queue.claim(run)
    from app.services.business_command_service import BusinessCommandService

    BusinessCommandService(factory, clock=clock).execute(run, owner, 1)
    queue.release(run, owner)
    service = NotificationService(factory, clock=clock)
    for _ in range(3):
        clock.advance(10)
        token = service.claim(f"turn:{run}:reply")
        assert token
        service.retry(f"turn:{run}:reply", token)
    status = GraphStatusService(factory).get(sid, buyer, run)
    assert status["notification_status"] == "MANUAL_REQUIRED"
    assert status["business_status"] == "COMMITTED"
