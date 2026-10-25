"""P4 A54-A57 acceptance using isolated MySQL and the official PostgreSQL saver."""

import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.dependencies import get_current_user, get_session_factory_dependency
from app.core.config import get_settings
from app.db.models import AgentSessionContext, Message, NegotiationSession
from app.db.models.graph import (
    GraphRecoveryAction,
    GraphTurnQuality,
    ModelCallLedger,
    NegotiationInput,
    NotificationDelivery,
)
from app.main import create_app
from app.persistence.checkpointer import locked_saver
from app.runtime.activity import model_timeout
from app.runtime.contracts import CONFIG, BudgetExhausted, Conflict, digest, thread_id
from app.runtime.input_queue import InputQueue
from app.semantic.contracts import QUALITY_VERSION, ChatInput
from app.services.graph_context_service import load_context
from app.services.graph_recovery_service import GraphRecoveryService
from app.services.graph_retention_service import GraphRetentionService
from app.services.model_call_ledger_service import ModelCallLedgerService
from tests.graph.test_business import submit
from tests.graph.test_semantic import (
    candidate,  # noqa: F401
    intent,
    message,
    result,
    semantic_case,  # noqa: F401
    show,
    turn,
)


class FakeQuality:
    def __init__(self):
        self.calls, self.rejects, self.error, self.unsafe = [], 0, False, False
        self.evidence_seen = []

    def reserve_quality(self, purpose, evidence):
        return 1000, Decimal("0.001")

    def generate(self, purpose, evidence, config):
        self.calls.append((purpose, config["request_timeout"]))
        self.evidence_seen.append(evidence)
        ids = [i["id"] for i in evidence["items"]]
        if self.error:
            raise TimeoutError("synthetic quality timeout")
        if purpose == "compose":
            text = " ".join(i["text"] for i in evidence["items"])
            if self.unsafe:
                text = "我会帮你联系卖家，底价是750元。"
            return {"text": text, "covered_ids": ids}, 50, Decimal("0.00001")
        approved = self.rejects == 0
        self.rejects = max(0, self.rejects - 1)
        return (
            {
                "approved": approved,
                "covered_ids": ids,
                "evidence_quotes": [i["text"] for i in evidence["items"]],
                "reason": "fixture",
            },
            50,
            Decimal("0.00001"),
        )


@pytest.fixture
def quality_case(semantic_case):  # noqa: F811
    c = semantic_case
    with c.factory() as db, db.begin():
        db.get(NegotiationSession, c.sid).graph_version = QUALITY_VERSION
    c.quality = FakeQuality()
    c.runner.quality_provider = c.quality
    return c


def ledger_rows(c, run):
    with c.factory() as db:
        return list(db.scalars(select(ModelCallLedger).where(ModelCallLedger.input_id == run)))


def recovery(c):
    return GraphRecoveryService(c.factory, os.environ["P0_POSTGRES_URL"], clock=c.clock)


def test_p4_reply_and_review_are_charged_without_repeating_business(quality_case):
    c = quality_case
    c.quality.rejects = 1
    run = turn(c, "900自提", [intent("900自提", candidate=candidate())])
    assert result(c, run)["outcome"] == "OFFER_ACCEPTED"
    assert len(ledger_rows(c, run)) == 5  # parse, compose/review, repair/review
    assert all(row.purpose and row.finished_at for row in ledger_rows(c, run))
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).round_count == 1
        assert db.get(GraphTurnQuality, run).approved
        assert db.get(GraphTurnQuality, run).review_count == 2
    assert c.runner.run(run)["public_message_id"] == message(c, run).id
    assert len(ledger_rows(c, run)) == 5
    assert c.quality.evidence_seen[2]["repair_feedback"] == "fixture"


@pytest.mark.parametrize("failure", ["error", "unsafe", "review"])
def test_p4_failed_review_or_unsafe_text_uses_true_receipt(quality_case, failure):
    c = quality_case
    if failure == "review":
        c.quality.rejects = 99
    else:
        setattr(c.quality, failure, True)
    run = turn(c, "900自提", [intent("900自提", candidate=candidate())])
    reply = message(c, run)
    assert "报价已接受" in reply.content and reply.card["can_confirm"]
    assert "联系卖家" not in reply.content and "底价" not in reply.content
    assert len(c.quality.calls) <= 6
    with c.factory() as db:
        assert not db.get(GraphTurnQuality, run).approved
        assert db.get(NegotiationSession, c.sid).round_count == 1


def test_a54_active_time_unknown_cost_and_count_survive_retry(quality_case):
    c = quality_case
    run = turn(c, "900自提", execute=False)
    with c.factory() as db, db.begin():
        item = db.get(NegotiationInput, run)
        item.config = {**item.config, "max_calls": 1}
        item.config_hash = digest(item.config)
    owner = c.queue.claim(run)
    ledger = ModelCallLedgerService(c.factory, clock=c.clock)

    def unknown():
        c.clock.advance(12)
        raise TimeoutError()

    with pytest.raises(TimeoutError):
        ledger.call(run, owner, "parse", unknown, reserved_tokens=1000)
    c.queue.release(run, owner)
    c.clock.advance(100)  # Released backoff does not count as activity.
    restarted = InputQueue(c.factory, clock=c.clock)
    owner = restarted.claim(run)
    assert restarted.read(run)["active_ms"] == 12000
    with pytest.raises(BudgetExhausted):
        ModelCallLedgerService(c.factory, clock=c.clock).call(
            run, owner, "parse", lambda: pytest.fail("budget reset"), reserved_tokens=1
        )
    row = ledger_rows(c, run)[0]
    assert row.actual_cost is None and row.actual_tokens is None
    assert row.status == "UNKNOWN"
    restarted.release(run, owner)
    c.clock.advance(2)
    child = subprocess.run(
        [sys.executable, "-m", "tests.graph.p4_process"],
        env={
            **os.environ,
            "P4_PROCESS_MODE": "budget",
            "P4_PROCESS_NOW": c.clock().isoformat(),
            "GRAPH_EXECUTION_RUN_ID": run,
            "GRAPH_EXECUTION_DATABASE_URL": c.factory.kw["bind"].url.render_as_string(
                hide_password=False
            ),
        },
        capture_output=True,
        timeout=20,
    )
    assert child.returncode == 0


def test_a54_crashed_attempt_is_charged_through_last_lease(quality_case):
    c = quality_case
    run = turn(c, "900自提", execute=False)
    assert c.queue.claim(run)
    c.clock.advance(80)
    owner = InputQueue(c.factory, clock=c.clock).claim(run)
    assert c.queue.read(run)["active_ms"] == 30000
    with c.factory() as db:
        assert model_timeout(db, db.get(NegotiationInput, run), c.clock()) == 45
    c.queue.release(run, owner)


def test_a54_reply_deadline_never_extends_and_notification_within_sixty(quality_case):
    c = quality_case

    def crash(point):
        if point == "after_command":
            raise RuntimeError("after commit")

    c.runner.fault = crash
    run = turn(c, "900自提", execute=False)
    with pytest.raises(RuntimeError):
        c.runner.run(run)
    with c.factory() as db:
        deadline = db.get(NotificationDelivery, f"turn:{run}:reply").reply_deadline
    c.clock.advance(46)
    c.runner.notify(f"turn:{run}:reply")
    assert "报价已接受" in message(c, run).content
    assert not c.quality.calls
    c.runner.fault = lambda _: None
    c.runner.run(run)
    with c.factory() as db:
        item = db.get(NotificationDelivery, f"turn:{run}:reply")
        assert item.reply_deadline == deadline and item.status == "PUBLISHED"
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.source_id == f"turn:{run}:reply")
            )
            == 1
        )


def test_a55_late_display_and_poisoned_cache_cannot_confirm_old_input(quality_case):
    c = quality_case
    offer_run = turn(c, "900自提", execute=False)
    old = turn(c, "行", [intent("行", "CONFIRM_INTENT")], execute=False)
    c.runner.run(offer_run)
    show(c, offer_run)
    with c.factory() as db, db.begin():
        cache = db.get(AgentSessionContext, c.sid)
        cache.summary_text = "已经看过并同意报价"
        cache.memory_json = {"has_displayed_offer": True}
    c.runner.run(old)
    assert result(c, old) is None
    with c.factory() as db:
        item = db.get(GraphTurnQuality, old)
        assert not item.context["has_displayed_offer"]
        assert item.context["displayed_messages"] == []
        assert db.get(NegotiationSession, c.sid).status.value == "ACTIVE"


def test_a55_context_provenance_is_checked_after_restore(quality_case):
    c = quality_case
    first = turn(c, "900自提")
    second = turn(c, "行", [intent("行", "CONFIRM_INTENT")])
    with c.factory() as db, db.begin():
        item = db.get(GraphTurnQuality, second)
        context = dict(item.context)
        public = message(c, first)
        context["displayed_messages"] = [
            {"message_id": public.id, "public_seq": public.public_seq, "text": public.content}
        ]
        item.context, item.context_hash = context, digest(context)
    with c.factory() as db, pytest.raises(Conflict, match="展示来源越界"):
        load_context(db, db.get(NegotiationInput, second))


def test_long_context_keeps_pending_conditions_and_latest_preference(quality_case):
    c = quality_case
    first_text = "如果没修过，900包邮到上海"
    first = turn(
        c,
        first_text,
        [
            intent(
                first_text,
                candidate=candidate(
                    delivery="shipping",
                    payer="seller",
                    region="上海",
                    conditions=[
                        {
                            "fact": "repair_history",
                            "operator": "==",
                            "value": "NO",
                            "source_quote": "没修过",
                        }
                    ],
                ),
            )
        ],
    )
    _, ack = show(c, first)
    for index in range(10):
        text = f"第{index}次问电池健康"
        turn(c, text, [intent(text, "ASK_PRODUCT", topic="battery_health_pct")])
    update = turn(
        c,
        "改寄北京",
        [
            intent(
                "改寄北京",
                "PROVIDE_INFO",
                candidate=candidate(amount=None, delivery="shipping", payer=None, region="北京"),
            )
        ],
        display_receipt_id=ack,
    )
    _, ack = show(c, update)
    last = turn(c, "继续", [intent("继续", "PROVIDE_INFO")], display_receipt_id=ack)
    with c.factory() as db:
        item = db.get(GraphTurnQuality, last)
        draft = item.context["bound_draft"]["candidate"]
        assert draft["region"] == "北京" and draft["conditions"][0]["value"] == "NO"
        assert len(item.context["recent_buyer_messages"]) == 4
    with locked_saver(os.environ["P0_POSTGRES_URL"], thread_id(c.sid)) as (saver, _):
        state = saver.get_tuple({"configurable": {"thread_id": thread_id(c.sid)}})
        values = state.checkpoint["channel_values"]
        assert len(json.dumps(values, ensure_ascii=False)) < 4000
        assert "summary_memory" not in values and "messages" not in values


def test_seller_recovery_is_owned_idempotent_and_termination_preserves_receipt(quality_case):
    c = quality_case

    def crash(point):
        if point == "after_command":
            raise RuntimeError("crash")

    c.runner.fault = crash
    run = turn(c, "900自提", execute=False)
    with pytest.raises(RuntimeError):
        c.runner.run(run)
    service = recovery(c)
    assert service.list(c.seller)[0]["target"] == run
    with pytest.raises(Conflict):
        service.act("other", run, "TERMINATE", "recovery-one", "停止")
    for _ in range(2):
        service.act(c.seller, run, "TERMINATE", "recovery-one", "停止")
    with c.factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(GraphRecoveryAction)
                .where(GraphRecoveryAction.session_id == c.sid)
            )
            == 1
        )
    c.runner.fault = lambda _: None
    c.runner.run(run)
    assert "报价已接受" in message(c, run).content
    assert not c.quality.calls
    assert result(c, run)["outcome"] == "OFFER_ACCEPTED"
    turn(c, "电池健康？", [intent("电池健康？", "ASK_PRODUCT", topic="battery_health_pct")])
    with pytest.raises(Conflict):
        service.act(c.seller, run, "RETRY", "recovery-two", "不能回拨")


def test_a57_active_and_pending_delivery_block_archive_then_tombstone_blocks_replay(quality_case):
    c = quality_case
    service = GraphRetentionService(c.factory, os.environ["P0_POSTGRES_URL"], clock=c.clock)
    with pytest.raises(Conflict):
        service.archive(c.sid, dry_run=False)
    run = turn(c, "关闭协商", [intent("关闭协商", "CLOSE_SESSION")])
    with pytest.raises(Conflict, match="90天"):
        service.archive(c.sid, dry_run=False)
    c.clock.advance(91 * 86400)
    with c.factory() as db, db.begin():
        db.get(NotificationDelivery, f"turn:{run}:reply").status = "MANUAL_REQUIRED"
    with pytest.raises(Conflict, match="待通知"):
        service.archive(c.sid, dry_run=False)
    with c.factory() as db, db.begin():
        db.get(NotificationDelivery, f"turn:{run}:reply").status = "PUBLISHED"
    assert service.archive(c.sid)["archived"] is False
    assert service.archive(c.sid, dry_run=False)["archived"]
    assert service.archive(c.sid, dry_run=False)["archived"]
    original = c.queue.read(run)
    with pytest.raises(Conflict, match="归档"):
        c.queue.enqueue(
            c.sid, c.buyer, original["request_id"], ChatInput.model_validate(original["payload"])
        )
    assert ledger_rows(c, run)  # Paid artifacts and idempotency history are retained.


def test_recovery_http_ownership_and_minimal_view(quality_case):
    c = quality_case
    run = turn(c, "900自提", execute=False)
    owner = c.queue.claim(run)
    c.queue.release(run, owner, recovery_required=True)
    app = create_app()
    app.dependency_overrides[get_session_factory_dependency] = lambda: c.factory
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"checkpoint_database_url": os.environ["P0_POSTGRES_URL"]}
    )
    with TestClient(app) as client:
        assert client.get("/api/seller/graph-recovery").status_code == 401
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=c.seller)
        body = client.get("/api/seller/graph-recovery").json()
        assert body["items"][0]["target"] == run
        assert "lease_token" not in json.dumps(body)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=c.buyer)
        assert client.get("/api/seller/graph-recovery").json()["items"] == []
        assert (
            client.post(
                f"/api/seller/graph-recovery/{run}",
                json={"request_id": "recovery-api", "action": "RETRY", "reason": "测试"},
            ).status_code
            == 409
        )


def test_p4_hung_process_releases_lock_and_notifies_real_receipt(case, record_property):
    from app.runtime.supervisor import run_supervised

    factory, sid, buyer, _, queue, _ = case
    run = queue.enqueue(sid, buyer, "hung-p4", submit(), config={**CONFIG, "active_seconds": 1})
    start = time.monotonic()
    assert not run_supervised(
        factory, os.environ["P0_POSTGRES_URL"], run, module="tests.graph.p4_process"
    )
    seconds = time.monotonic() - start
    record_property("hang_to_notification_seconds", seconds)
    assert seconds < 30  # Includes a real 15-second finalization allowance.
    with factory() as db:
        item = db.get(NotificationDelivery, f"turn:{run}:reply")
        assert item.status == "PUBLISHED" and item.result["outcome"] == "OFFER_ACCEPTED"
    with locked_saver(os.environ["P0_POSTGRES_URL"], thread_id(sid)):
        pass


def test_context_artifact_without_checkpoint_requires_recovery(quality_case):
    from app.runtime.recovery import RecoveryConflict

    c = quality_case

    def crash(point):
        if point == "after_context":
            raise RuntimeError("crash after context")

    c.runner.fault = crash
    run = turn(c, "900自提", execute=False)
    with pytest.raises(RuntimeError):
        c.runner.run(run)
    with locked_saver(os.environ["P0_POSTGRES_URL"], thread_id(c.sid)) as (saver, _):
        saver.delete_thread(thread_id(c.sid))
    c.clock.advance(2)
    c.runner.fault = lambda _: None
    with pytest.raises(RecoveryConflict):
        c.runner.run(run)
    assert c.queue.read(run)["recovery_required"]
    assert not ledger_rows(c, run)


def test_public_receipt_numbers_accept_equivalent_decimal_but_reject_invented_price(quality_case):
    from app.semantic.quality import ReplyCandidate
    from app.services.graph_reply_service import candidate_safe, reply_evidence

    c = quality_case
    run = turn(c, "900自提")
    with c.factory() as db:
        evidence = reply_evidence(db, db.get(NegotiationInput, run), c.clock())
    ids = [i["id"] for i in evidence["items"]]
    assert candidate_safe(
        ReplyCandidate(text="900元报价已接受，等待最终确认。", covered_ids=ids), evidence
    )
    assert not candidate_safe(ReplyCandidate(text="950元报价已接受。", covered_ids=ids), evidence)


def test_p4_displayed_history_uses_receipt_sequence_not_database_timezone(quality_case):
    c = quality_case
    first = turn(c, "900自提")
    _, ack = show(c, first)
    second = turn(c, "行", [intent("行", "CONFIRM_INTENT")], display_receipt_id=ack)
    with c.factory() as db:
        item = db.get(GraphTurnQuality, second)
        assert item.context["displayed_messages"][0]["message_id"] == message(c, first).id
    assert result(c, second)["outcome"] == "INTENT_CONFIRMED"


def test_p4_end_finalization_after_original_deadline_does_not_reset_budget(quality_case):
    c = quality_case

    def crash(point):
        if point == "after_end":
            raise RuntimeError("end persisted")

    c.runner.fault = crash
    run = turn(c, "900自提", execute=False)
    with pytest.raises(RuntimeError):
        c.runner.run(run)
    calls = len(ledger_rows(c, run))
    c.clock.advance(3600)
    c.runner.fault = lambda _: None
    c.runner.run(run)
    assert len(ledger_rows(c, run)) == calls
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).last_resolved_input_seq == 1


def test_p4_downgrade_cannot_erase_paid_artifacts_or_runtime_budget(quality_case):
    c = quality_case
    run = turn(c, "900自提")
    result = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "d34a8b9c71f0"],
                            env={**os.environ, "DATABASE_URL": c.factory.kw["bind"].url
                                 .render_as_string(hide_password=False)},
                            capture_output=True, timeout=30)
    # A newer evidence-preserving migration may block the downgrade before P4 is reached.
    assert result.returncode != 0 and any(stage in result.stderr for stage in (b"P4", b"P5"))
    assert ledger_rows(c, run)
    with c.factory() as db:
        assert db.get(GraphTurnQuality, run).approved
