"""D4 deterministic replay of semantic failures with real authorization and receipts."""

import pytest

from app.db.models import NegotiationSession
from app.db.models.graph import GraphTurnQuality, NegotiationInput
from app.semantic.contracts import ChatInput
from tests.graph.test_p4_quality import quality_case  # noqa: F401
from tests.graph.test_semantic import (
    candidate,
    intent,
    message,
    result,
    semantic_case,  # noqa: F401
    show,
    turn,
)


@pytest.mark.parametrize(
    ("text", "quote"),
    [
        ("我出800元，自提，现在提交这个报价。", "现在提交这个报价"),
        ("请提交我的报价：800元自提。", "请提交我的报价"),
        ("先别确认，我还要考虑。", "确认"),
        ("我不确认成交，只是问一下现在的报价。", "确认成交"),
        ("我不同意。", "同意"),
        ("不接受。", "接受"),
    ],
)
def test_d4_bad_model_confirmation_never_commits(quality_case, text, quote):  # noqa: F811
    c = quality_case
    first = turn(c, "900自提")
    card, ack = show(c, first)
    run = turn(c, text, [intent(quote, "CONFIRM_INTENT")], display_receipt_id=ack)
    assert result(c, run) is None
    with c.factory() as db:
        session = db.get(NegotiationSession, c.sid)
        assert session.status.value != "AGREED"
        assert session.current_offer_id == card["offer_id"]
        assert session.round_count == 1


def test_d4_conflicting_model_actions_still_clarify(quality_case):  # noqa: F811
    c = quality_case
    first = turn(c, "900自提")
    _, ack = show(c, first)
    text = "我出800元，自提，现在提交这个报价。"
    run = turn(
        c,
        text,
        [
            {**intent("我出800元，自提", candidate=candidate("800")), "id": "offer"},
            {**intent("现在提交这个报价", "CONFIRM_INTENT"), "id": "confirm"},
        ],
        display_receipt_id=ack,
    )
    assert result(c, run) is None
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).round_count == 1


@pytest.mark.parametrize("invalid", ["candidate_quote", "missing_id"])
def test_d4_review_cannot_self_cite_or_omit_intents(
    quality_case,  # noqa: F811
    invalid,
    monkeypatch,
):
    c = quality_case
    original = c.quality.generate

    def generate(purpose, evidence, config):
        value, tokens, cost = original(purpose, evidence, config)
        if purpose == "compose":
            value["text"] = "已收到。" + value["text"]
        elif invalid == "candidate_quote":
            value["evidence_quotes"] = [evidence["candidate"]]
        else:
            value["covered_ids"] = []
        return value, tokens, cost

    monkeypatch.setattr(c.quality, "generate", generate)
    run = turn(c, "900自提")
    assert result(c, run)["outcome"] == "OFFER_ACCEPTED"
    assert "已收到" not in message(c, run).content
    with c.factory() as db:
        assert not db.get(GraphTurnQuality, run).approved
        assert db.get(GraphTurnQuality, run).stop_reason == "REVIEW_REJECTED"
        assert db.get(NegotiationSession, c.sid).round_count == 1


def test_d4_new_defaults_do_not_rewrite_queued_config_or_replay_budget(
    quality_case,  # noqa: F811
    monkeypatch,
):
    c = quality_case
    with monkeypatch.context() as old:
        old.setattr("app.semantic.model.P4_PROMPT_VERSION", "p5-intents-v1")
        old.setattr("app.semantic.quality.QUALITY_PROMPT_VERSION", "p4-v2")
        run = turn(c, "900自提", execute=False)
    with c.factory() as db:
        row = db.get(NegotiationInput, run)
        request, payload, frozen_hash = row.request_id, row.payload, row.config_hash
        assert row.config["prompt_version"] == "p5-intents-v1"
        assert row.config["quality_version"] == "p4-v2"
    assert c.queue.enqueue(c.sid, c.buyer, request, ChatInput.model_validate(payload)) == run
    c.runner.run(run)
    calls = (c.model.calls, len(c.quality.calls))
    c.runner.run(run)
    assert (c.model.calls, len(c.quality.calls)) == calls
    with c.factory() as db:
        assert db.get(NegotiationInput, run).config_hash == frozen_hash
    current = turn(c, "800自提", [intent("800自提", candidate=candidate("800"))])
    with c.factory() as db:
        assert db.get(NegotiationInput, current).config["prompt_version"] == "p5-intents-v3"
        assert db.get(NegotiationInput, current).config["quality_version"] == "p4-v3"


def test_d4_correct_offer_is_submitted_once_and_independently_confirmed(
    quality_case,  # noqa: F811
):
    c = quality_case
    text = "我出900元，自提，现在提交这个报价。"
    run = turn(c, text, [intent(text, candidate=candidate())])
    assert result(c, run)["outcome"] == "OFFER_ACCEPTED"
    before_calls = (c.model.calls, len(c.quality.calls))
    c.runner.run(run)
    assert (c.model.calls, len(c.quality.calls)) == before_calls
    _, ack = show(c, run)
    final = turn(
        c,
        "我确认按已展示报价成交",
        [
            intent("我确认按已展示报价成交", "CONFIRM_INTENT"),
        ],
        display_receipt_id=ack,
    )
    assert result(c, final)["outcome"] == "INTENT_CONFIRMED"
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).round_count == 1
