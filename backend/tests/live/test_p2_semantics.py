"""Development acceptance, not the independent P5 risk holdout or production permission."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.db.models import Message, NegotiationSession, Offer
from app.db.models.semantic import OfferDraft, SemanticOfferBasis
from app.semantic.contracts import ChatInput
from app.semantic.maintenance import EvidenceMaintenance
from app.semantic.presentation import refresh_context
from app.services.notification_service import NotificationService
from tests.graph.test_semantic import fact, message, result, rule, show


def turn(c, text, *, execute=True, **refs):
    run = c.queue.enqueue(
        c.sid, c.buyer, uuid4().hex, ChatInput(text=text, client_view_id="tab", **refs)
    )
    if execute:
        c.runner.run(run)
        assert message(c, run) is not None
    return run


def rounds(c):
    with c.factory() as db:
        return db.get(NegotiationSession, c.sid).round_count


@pytest.mark.parametrize(
    "price,outcome",
    [
        ("900", "OFFER_ACCEPTED"),
        ("800", "WAITING_APPROVAL"),
        ("700", "OFFER_REJECTED"),
    ],
)
def test_price_ranges(live_case, price, outcome):
    c = live_case
    run = turn(c, f"{price}自提可以吗？")
    assert result(c, run)["outcome"] == outcome
    assert rounds(c) == 1


@pytest.mark.parametrize(
    "text",
    [
        "预算大概900，先看看",
        "别人卖900，我预算也差不多",
        "我不接受900自提",
        "只是比价900，不报价",
    ],
)
def test_information_and_negation_never_submit(live_case, text):
    c = live_case
    assert result(c, turn(c, text)) is None
    assert rounds(c) == 0


@pytest.mark.parametrize("battery,accepted", [(None, False), ("89", False), ("95", True)])
def test_conditional_offer(live_case, battery, accepted):
    c = live_case
    fact(c, "repair_history", "NO")
    if battery:
        fact(c, "battery_health_pct", battery)
    run = turn(c, "如果电池至少90%而且没修过，900自提")
    assert (result(c, run) is not None) == accepted
    if not accepted:
        assert message(c, run).card["kind"] == "drafts"


def test_independent_questions_and_offer(live_case):
    c = live_case
    fact(c, "repair_history", "NO")
    run = turn(c, "电池多少？维修过吗？900自提行吗？")
    assert result(c, run)["outcome"] == "OFFER_ACCEPTED"
    reply = message(c, run)
    assert len(reply.card["intent_results"]) == 3
    assert "尚未提供" in reply.content and "无维修记录" in reply.content


def test_short_continuation_retains_condition(live_case):
    c = live_case
    fact(c, "battery_health_pct", "95")
    first = turn(c, "如果电池至少90%，900可以吗？")
    assert rounds(c) == 0
    card, ack = show(c, first)
    second = turn(c, "自提", display_receipt_id=ack)
    assert result(c, second)["outcome"] == "OFFER_ACCEPTED"
    with c.factory() as db:
        basis = db.get(SemanticOfferBasis, result(c, second)["offer_id"])
        assert basis.candidate["amount"] == "900"
        assert basis.candidate["conditions"][0]["value"] == "90"
        assert db.get(OfferDraft, card["drafts"][0]["draft_id"]).version == 2


def test_alternatives_then_selection_keeps_unknown_condition(live_case):
    c = live_case
    first = turn(c, "电池至少90%的话，900自提或者950包邮")
    assert result(c, first) is None and rounds(c) == 0
    card, ack = show(c, first)
    assert len(card["drafts"]) == 2
    chosen = turn(c, "选第一个", display_receipt_id=ack)
    assert result(c, chosen) is None
    draft = message(c, chosen).card["drafts"][0]
    assert draft["candidate"]["amount"] == "900"
    assert draft["candidate"]["conditions"][0]["value"] == "90"


@pytest.mark.parametrize("kind", ["fact", "cost"])
def test_seller_supplement_waits_for_buyer(live_case, kind):
    c = live_case
    text = "如果没修过，900自提" if kind == "fact" else "900包邮到上海可以吗？"
    first = turn(c, text)
    assert result(c, first) is None
    inspection = EvidenceMaintenance(c.factory, clock=c.clock).inspect(c.pid, c.seller)
    assert inspection["verifications"][0]["status"] == "OPEN"
    if kind == "fact":
        fact(c, "repair_history", "NO")
    else:
        rule(c)
    assert rounds(c) == 0
    with c.factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Offer).where(Offer.session_id == c.sid)) == 0
        )
        ready = db.scalar(
            select(Message).where(
                Message.session_id == c.sid, Message.source_id.like("draft-ready:%")
            )
        )
        assert "尚未提交" in ready.content
    _, ack = show(c, first)
    final = turn(c, "继续", display_receipt_id=ack)
    assert result(c, final)["outcome"] == "OFFER_ACCEPTED"


def test_confirmation_after_visible_offer(live_case):
    c = live_case
    first = turn(c, "900自提可以吗？")
    _, ack = show(c, first)
    final = turn(c, "行", display_receipt_id=ack)
    assert result(c, final)["outcome"] == "INTENT_CONFIRMED"


def test_changed_terms_do_not_confirm(live_case):
    c = live_case
    first = turn(c, "900自提可以吗？")
    _, ack = show(c, first)
    changed = turn(c, "行，但是得包邮", display_receipt_id=ack)
    assert result(c, changed) is None
    draft = message(c, changed).card["drafts"][0]["candidate"]
    assert draft["amount"] == "900" and draft["delivery"] == "shipping"
    assert draft["payer"] == "seller" and draft["region"] is None
    assert rounds(c) == 1


def test_refresh_does_not_authorize_earlier_input(live_case):
    c = live_case
    first = turn(c, "900自提可以吗？")
    card, _ = show(c, first)
    late = turn(c, "行", execute=False)
    c.clock.advance(1801)
    fresh = refresh_context(c.factory, c.sid, c.buyer, card["context_id"], "refresh", c.clock())
    ack = NotificationService(c.factory, clock=c.clock).display(
        c.sid, c.buyer, fresh["context_id"], "tab", "new-display"
    )
    c.runner.run(late)
    assert result(c, late) is None
    final = turn(c, "确认", display_receipt_id=ack)
    assert result(c, final)["outcome"] == "INTENT_CONFIRMED"


def test_region_change_requires_new_cost(live_case):
    c = live_case
    rule(c)
    first = turn(c, "900包邮到上海可以吗？")
    _, ack = show(c, first)
    changed = turn(c, "改寄北京", display_receipt_id=ack)
    assert result(c, changed) is None
    assert message(c, changed).card["drafts"][0]["candidate"]["region"] == "北京"


def test_correction_uses_only_revised_price(live_case):
    c = live_case
    run = turn(c, "不是800，我说的是900自提")
    assert result(c, run)["outcome"] == "OFFER_ACCEPTED"
    with c.factory() as db:
        assert db.get(Offer, result(c, run)["offer_id"]).price == 900


@pytest.mark.parametrize("text", ["900自提，但今天必须发货", "不买了结束吧，同时900自提继续报价"])
def test_unsupported_or_conflicting_action(live_case, text):
    c = live_case
    assert result(c, turn(c, text)) is None
    assert rounds(c) == 0


def test_rejection_is_not_withdrawal_of_existing_offer(live_case):
    c = live_case
    first = turn(c, "900自提可以吗？")
    card, ack = show(c, first)
    rejected = turn(c, "我不接受900自提", display_receipt_id=ack)
    assert result(c, rejected) is None
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).current_offer_id == card["offer_id"]


def test_local_condition_does_not_block_other_alternative(live_case):
    c = live_case
    first = turn(c, "900自提，或者如果电池至少90%就950包邮到上海")
    assert result(c, first) is None
    card, ack = show(c, first)
    assert len(card["drafts"]) == 2
    assert card["drafts"][0]["candidate"]["conditions"] == []
    assert card["drafts"][1]["candidate"]["conditions"][0]["value"] == "90"
    chosen = turn(c, "选第一个", display_receipt_id=ack)
    assert result(c, chosen)["outcome"] == "OFFER_ACCEPTED"


def test_explicit_withdrawal_and_close(live_case):
    c = live_case
    first = turn(c, "900自提可以吗？")
    _, ack = show(c, first)
    withdrew = turn(c, "先撤回刚才的报价，顺便问下维修过吗？", display_receipt_id=ack)
    assert result(c, withdrew)["outcome"] == "OFFER_WITHDRAWN"
    assert len(message(c, withdrew).card["intent_results"]) == 2
    closed = turn(c, "不买了，结束吧")
    assert result(c, closed)["outcome"] == "SESSION_CLOSED"
