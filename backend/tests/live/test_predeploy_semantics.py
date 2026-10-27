"""D4 opt-in development checks; never an independent semantic quality evaluation."""

import hashlib
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import NegotiationSession
from app.db.models.graph import GraphTurnQuality, NegotiationInput, PublicInteractionContext
from app.db.models.semantic import SemanticTurn
from app.runtime.contracts import Conflict, utcnow
from app.semantic.contracts import QUALITY_VERSION
from app.semantic.model import P4_PROMPT_VERSION, SemanticResponseError
from app.semantic.quality import QUALITY_PROMPT_VERSION, QualityModel
from tests.graph.test_semantic import fact, message, result, rule, show
from tests.live.test_p2_semantics import turn


@pytest.fixture
def d4_live(live_case, live_evidence, request):
    c = live_case
    c.clock = c.queue.clock = utcnow
    with c.factory() as db, db.begin():
        db.get(NegotiationSession, c.sid).graph_version = QUALITY_VERSION
    live_evidence.report.update(
        stage="D4",
        prompt_version=P4_PROMPT_VERSION,
        quality_version=QUALITY_PROMPT_VERSION,
        scope="development regression, not independent generalization or human evaluation",
        price_basis="existing application estimate, not provider bill or updated tariff",
    )
    root = Path(__file__).resolve().parents[3]
    live_evidence.report["source_hashes"] = {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in (
            "backend/app/semantic/model.py",
            "backend/app/semantic/service.py",
            "backend/app/semantic/quality.py",
            "backend/app/runtime/input_queue.py",
            "backend/app/services/graph_reply_service.py",
            "backend/tests/live/test_predeploy_semantics.py",
            "backend/tests/live/conftest.py",
        )
    }

    class RecordedQuality(QualityModel):
        def generate(self, purpose, evidence, config):
            attempt = {
                "case": request.node.name,
                "purpose": purpose,
                "model": config["model_name"],
                "quality_version": config["quality_version"],
                "request_timeout": config["request_timeout"],
                "status": "STARTED",
                "evidence": evidence,
            }
            live_evidence.report["attempts"].append(attempt)
            live_evidence.save()
            started = time.monotonic()
            try:
                value, tokens, cost = super().generate(purpose, evidence, config)
                attempt.update(
                    status="OK",
                    value=value,
                    tokens=tokens,
                    estimated_cost=str(cost) if cost is not None else None,
                )
                return value, tokens, cost
            except Exception as exc:
                attempt.update(status="FAILED", error_type=type(exc).__name__)
                if isinstance(exc, SemanticResponseError):
                    attempt.update(
                        raw_content=exc.raw_content,
                        tokens=exc.tokens,
                        estimated_cost=str(exc.cost) if exc.cost is not None else None,
                    )
                raise
            finally:
                attempt["seconds"] = round(time.monotonic() - started, 3)
                live_evidence.save()

    c.runner.quality_provider = RecordedQuality()
    started = time.monotonic()
    yield c
    # Existing live_case teardown persists receipts, states, ledgers and published replies.
    with c.factory() as db:
        observations = []
        for run in db.scalars(
            select(NegotiationInput)
            .where(
                NegotiationInput.session_id == c.sid,
            )
            .order_by(NegotiationInput.input_seq)
        ):
            semantic = db.get(SemanticTurn, run.id)
            quality = db.get(GraphTurnQuality, run.id)
            observations.append(
                {
                    "run_id": run.id,
                    "config": run.config,
                    "frame": semantic.frame if semantic else None,
                    "results": semantic.results if semantic else None,
                    "command": semantic.command["kind"] if semantic else None,
                    "quality_approved": quality.approved if quality else None,
                    "stop_reason": quality.stop_reason if quality else None,
                    "review_feedback": quality.review_feedback if quality else None,
                }
            )
    live_evidence.report.setdefault("d4_observations", []).append(
        {
            "case": request.node.name,
            "seconds": round(time.monotonic() - started, 3),
            "runs": observations,
        }
    )
    live_evidence.save()


@pytest.mark.parametrize(
    "text",
    [
        "我出800元，自提，现在提交这个报价。",
        "请提交我的报价：800元自提。",
        "800元自提，这就是我的报价，麻烦提交。",
    ],
)
def test_d4_submit_offer_is_not_final_confirmation(d4_live, text):
    c = d4_live
    run = turn(c, text)
    assert result(c, run) is not None
    assert result(c, run)["outcome"] == "WAITING_APPROVAL"
    with c.factory() as db:
        session = db.get(NegotiationSession, c.sid)
        assert session.round_count == 1 and session.status.value != "AGREED"


@pytest.mark.parametrize("known", [True, False])
def test_d4_conditional_offer_preserves_fact_requirement(d4_live, known):
    c = d4_live
    if known:
        fact(c, "repair_history", "NO")
    run = turn(c, "如果没修过，我出900元自提，请提交这个报价。")
    if known:
        assert result(c, run)["outcome"] == "OFFER_ACCEPTED"
    else:
        assert result(c, run) is None
        draft = message(c, run).card["drafts"][0]
        assert "condition:repair_history:UNKNOWN" in draft["missing"]
        assert draft["candidate"]["conditions"][0]["value"] == "NO"


def test_d4_delivery_change_is_not_confirmation(d4_live):
    c = d4_live
    rule(c, region="上海")
    first = turn(c, "900自提可以吗？")
    _, ack = show(c, first)
    run = turn(c, "改成900元包邮到上海，请提交修改后的报价。", display_receipt_id=ack)
    assert result(c, run)["outcome"] == "OFFER_ACCEPTED"
    with c.factory() as db:
        session = db.get(NegotiationSession, c.sid)
        assert session.round_count == 2 and session.status.value != "AGREED"


@pytest.mark.parametrize(
    "text",
    [
        "我不确认成交，只是问一下现在的报价。",
        "先别确认，我还要考虑。",
        "确认按钮是什么意思？先别确认。",
        "我不同意。",
        "不接受。",
    ],
)
def test_d4_negative_or_question_does_not_confirm(d4_live, text):
    c = d4_live
    first = turn(c, "900自提可以吗？")
    _, ack = show(c, first)
    run = turn(c, text, display_receipt_id=ack)
    assert result(c, run) is None
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).status.value != "AGREED"


@pytest.mark.parametrize("display", ["valid", "expired", "expired_after_enqueue", "missing"])
def test_d4_confirmation_requires_current_display(d4_live, display):
    c = d4_live
    first = turn(c, "900自提可以吗？")
    card, ack = show(c, first)
    if display == "expired_after_enqueue":
        run = turn(c, "我确认按已展示的报价成交。", display_receipt_id=ack, execute=False)
    if display in {"expired", "expired_after_enqueue"}:
        with c.factory() as db, db.begin():
            db.get(PublicInteractionContext, card["context_id"]).expires_at = utcnow() - timedelta(
                seconds=1
            )
    if display == "expired":
        with pytest.raises(Conflict, match="展示凭据"):
            turn(c, "我确认按已展示的报价成交。", display_receipt_id=ack)
        with c.factory() as db:
            assert db.get(NegotiationSession, c.sid).status.value != "AGREED"
        return
    if display == "expired_after_enqueue":
        c.runner.run(run)
        assert message(c, run) is not None
    else:
        run = turn(
            c,
            "我确认按已展示的报价成交。",
            **({"display_receipt_id": ack} if display != "missing" else {}),
        )
    if display == "valid":
        assert result(c, run)["outcome"] == "INTENT_CONFIRMED"
    else:
        assert result(c, run) is None
        with c.factory() as db:
            assert db.get(NegotiationSession, c.sid).status.value != "AGREED"


def test_d4_budget_is_not_offer(d4_live):
    c = d4_live
    run = turn(c, "预算800元，先看看，暂不提交报价。")
    assert result(c, run) is None
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).round_count == 0
        assert db.get(GraphTurnQuality, run) is not None


def test_d4_new_submission_with_existing_display_does_not_confirm_old_offer(d4_live):
    c = d4_live
    first = turn(c, "900自提可以吗？")
    _, ack = show(c, first)
    run = turn(c, "请提交我的报价：800元自提。", display_receipt_id=ack)
    assert result(c, run)["outcome"] == "WAITING_APPROVAL"
    with c.factory() as db:
        session = db.get(NegotiationSession, c.sid)
        assert session.round_count == 2 and session.status.value != "AGREED"


def test_d4_actual_conflicting_offer_and_confirmation_stays_uncommitted(d4_live):
    c = d4_live
    first = turn(c, "900自提可以吗？")
    _, ack = show(c, first)
    run = turn(c, "改成800元自提，同时确认成交。", display_receipt_id=ack)
    assert result(c, run) is None
    with c.factory() as db:
        session = db.get(NegotiationSession, c.sid)
        assert session.round_count == 1 and session.status.value != "AGREED"
