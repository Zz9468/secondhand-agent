"""P2 acceptance on actual MySQL/PostgreSQL; model calls are local typed fixtures."""

import os
from datetime import UTC, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.dependencies import get_current_user, get_session_factory_dependency
from app.api.semantic import RuleRequest
from app.db.models import Message, NegotiationSession, Offer, Product
from app.db.models.graph import BusinessActionReceipt, ModelCallLedger, PublicInteractionContext
from app.db.models.semantic import OfferDraft, SemanticOfferBasis
from app.main import create_app
from app.runtime.contracts import Conflict
from app.runtime.graph_runner import GraphRunner
from app.runtime.input_queue import InputQueue
from app.semantic.contracts import VERSION, ChatInput, IntentFrame
from app.semantic.maintenance import EvidenceMaintenance
from app.semantic.presentation import refresh_context
from app.services.notification_service import NotificationService
from tests.graph.conftest import Clock
from tests.integration.factories import create_negotiation


class FakeSemanticModel:
    def __init__(self):
        self.values, self.calls = {}, 0

    def reserve(self, text, context):
        return 1000, Decimal("0.001")

    def __call__(self, text, context, config):
        self.calls += 1
        return self.values[text], 100, Decimal("0")


@pytest.fixture
def semantic_case(graph_factory):
    sid, buyer = create_negotiation(
        graph_factory, minimum_net_price=Decimal("750"), auto_accept_threshold=Decimal("850")
    )
    clock = Clock()
    queue = InputQueue(graph_factory, clock=clock)
    queue.enroll(sid, buyer, version=VERSION)
    model = FakeSemanticModel()
    runner = GraphRunner(
        graph_factory, os.environ["P0_POSTGRES_URL"], queue=queue, semantic_provider=model
    )
    with graph_factory() as db:
        product = db.get(Product, db.get(NegotiationSession, sid).product_id)
        pid, seller = product.id, product.seller_id
    case = SimpleNamespace(
        factory=graph_factory,
        sid=sid,
        buyer=buyer,
        clock=clock,
        queue=queue,
        model=model,
        runner=runner,
        pid=pid,
        seller=seller,
    )
    fact(case, "allowed_delivery_methods", ["pickup", "shipping"])
    return case


def fact(c, attribute, value):
    service = EvidenceMaintenance(c.factory, clock=c.clock)
    version = service.inspect(c.pid, c.seller)["product_version"]
    return service.set_fact(c.pid, c.seller, attribute, value, version)


def rule(c, cost="20", region="上海", rule_id=None, active=True):
    service = EvidenceMaintenance(c.factory, clock=c.clock)
    version = service.inspect(c.pid, c.seller)["product_version"]
    return service.set_rule(
        c.pid,
        c.seller,
        RuleRequest(
            expected_version=version,
            rule_id=rule_id,
            region=region,
            shipping_cost=cost,
            active=active,
            expires_at=(c.clock() + timedelta(days=2)).replace(tzinfo=UTC),
        ),
    )


def intent(text, kind="PROPOSE_OFFER", **fields):
    return {"id": "i", "type": kind, "source_quote": text, **fields}


def candidate(amount="900", delivery="pickup", **fields):
    return {
        "amount": amount,
        "delivery": delivery,
        "payer": "buyer" if delivery == "pickup" else "seller",
        **fields,
    }


def turn(c, text, intents=None, *, execute=True, **refs):
    c.model.values[text] = {"intents": intents or [intent(text, candidate=candidate())]}
    run = c.queue.enqueue(
        c.sid,
        c.buyer,
        uuid4().hex,
        ChatInput(text=text, client_view_id=refs.pop("client_view_id", "tab"), **refs),
    )
    if execute:
        c.runner.run(run)
    return run


def result(c, run):
    with c.factory() as db:
        receipt = db.get(BusinessActionReceipt, f"turn:{run}:action")
        return receipt.result if receipt else None


def message(c, run):
    with c.factory() as db:
        return db.scalar(select(Message).where(Message.source_id == f"turn:{run}:reply"))


def show(c, run):
    card = message(c, run).card
    receipt = NotificationService(c.factory, clock=c.clock).display(
        c.sid, c.buyer, card["context_id"], "tab", uuid4().hex
    )
    return card, receipt


@pytest.mark.parametrize(
    "amount,outcome",
    [("900", "OFFER_ACCEPTED"), ("800", "WAITING_APPROVAL"), ("700", "OFFER_REJECTED")],
)
def test_clear_chat_proposal_needs_no_formal_checkbox(semantic_case, amount, outcome):
    c = semantic_case
    run = turn(
        c, f"{amount}自提可以吗", [intent(f"{amount}自提可以吗", candidate=candidate(amount))]
    )
    assert result(c, run)["outcome"] == outcome
    assert c.model.calls == 1


def test_model_cannot_turn_rejection_into_withdrawal(semantic_case):
    c = semantic_case
    first = turn(c, "900自提")
    card, ack = show(c, first)
    text = "我不接受900自提"
    rejected = turn(c, text, [intent(text, "WITHDRAW_OFFER")], display_receipt_id=ack)
    assert result(c, rejected) is None
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).current_offer_id == card["offer_id"]


@pytest.mark.parametrize("patch", [None, {}])
def test_change_terms_without_current_patch_cannot_resubmit_previous_terms(semantic_case, patch):
    c = semantic_case
    first = turn(c, "900自提")
    card, ack = show(c, first)
    text = "行，但是得包邮"
    changed = turn(c, text, [intent(text, "CHANGE_TERMS", candidate=patch)], display_receipt_id=ack)
    assert result(c, changed) is None
    with c.factory() as db:
        session = db.get(NegotiationSession, c.sid)
        assert session.current_offer_id == card["offer_id"] and session.round_count == 1


@pytest.mark.parametrize("omit_condition", [False, True])
@pytest.mark.parametrize("whole_quote", [False, True])
def test_branch_local_condition_is_preserved_without_affecting_other_choice(
    semantic_case, omit_condition, whole_quote
):
    c = semantic_case
    text = "900自提，或者如果电池至少90%就950包邮到上海"
    frame = [
        {
            **intent(
                text if whole_quote else "900自提",
                candidate=candidate(),
                alternative_group="choices",
            ),
            "id": "a",
        },
        {
            **intent(
                "如果电池至少90%就950包邮到上海",
                alternative_group="choices",
                candidate=candidate(
                    "950",
                    "shipping",
                    region="上海",
                    conditions=[] if omit_condition else [battery_condition()],
                ),
            ),
            "id": "b",
        },
    ]
    first = turn(c, text, frame)
    assert result(c, first) is None
    if omit_condition:
        assert not (message(c, first).card or {}).get("drafts")
    else:
        card, ack = show(c, first)
        assert card["drafts"][0]["candidate"]["conditions"] == []
        chosen = turn(c, "选第一个", [intent("选第一个", "PROVIDE_INFO")], display_receipt_id=ack)
        assert result(c, chosen)["outcome"] == "OFFER_ACCEPTED"


@pytest.mark.parametrize("text", ["别人卖900", "预算900，先看看", "只是比价900，不报价"])
def test_budget_comparison_is_not_a_proposal(semantic_case, text):
    c = semantic_case
    run = turn(c, text, [intent(text, "PROVIDE_INFO", topic="budget", candidate=candidate())])
    assert result(c, run) is None
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).round_count == 0


def test_mixed_questions_are_independent_and_all_answered(semantic_case):
    c = semantic_case
    fact(c, "repair_history", "NO")
    text = "维修过吗？电池多少？900自提"
    run = turn(
        c,
        text,
        [
            {**intent("维修过吗", "ASK_PRODUCT", topic="repair_history"), "id": "q1"},
            {**intent("电池多少", "ASK_PRODUCT", topic="battery_health_pct"), "id": "q2"},
            intent("900自提", candidate=candidate()),
        ],
    )
    assert result(c, run)["outcome"] == "OFFER_ACCEPTED"
    content = message(c, run).content
    assert "无维修记录" in content and "尚未提供" in content and "报价已接受" in content
    assert len(message(c, run).card["intent_results"]) == 3


def battery_condition():
    return {
        "fact": "battery_health_pct",
        "operator": ">=",
        "value": "90",
        "source_quote": "电池至少90%",
    }


@pytest.mark.parametrize("value,accepted", [(None, False), ("89", False), ("95", True)])
def test_a39_structured_conditions_not_description(semantic_case, value, accepted):
    c = semantic_case
    with c.factory() as db, db.begin():
        db.get(Product, c.pid).description = "电池很好，几乎全新"
    if value:
        fact(c, "battery_health_pct", value)
    text = "如果电池至少90%，900自提"
    run = turn(c, text, [intent(text, candidate=candidate(conditions=[battery_condition()]))])
    assert (result(c, run) is not None) == accepted
    if not accepted:
        assert message(c, run).card["kind"] == "drafts"


def test_a37_short_answer_preserves_amount_and_all_conditions(semantic_case):
    c = semantic_case
    fact(c, "battery_health_pct", "95")
    text = "如果电池至少90%，900可以吗"
    first = turn(
        c, text, [intent(text, candidate={"amount": "900", "conditions": [battery_condition()]})]
    )
    card, ack = show(c, first)
    second = turn(
        c,
        "自提",
        [intent("自提", "PROVIDE_INFO", candidate={"delivery": "pickup", "payer": "buyer"})],
        display_receipt_id=ack,
    )
    assert result(c, second)["outcome"] == "OFFER_ACCEPTED"
    with c.factory() as db:
        basis = db.get(SemanticOfferBasis, result(c, second)["offer_id"])
        assert basis.candidate["amount"] == "900"
        assert basis.candidate["conditions"] == [battery_condition()]
        draft = db.get(OfferDraft, card["drafts"][0]["draft_id"])
        assert draft.version == 2 and len(draft.source_messages) == 2


@pytest.mark.parametrize("mode", ["unshown", "late", "expired", "different_tab"])
def test_a38_a49_short_answer_never_uses_latest_draft(semantic_case, mode):
    c = semantic_case
    first = turn(c, "900可以吗", [intent("900可以吗", candidate={"amount": "900"})])
    refs = {}
    if mode in {"expired", "different_tab"}:
        _, ack = show(c, first)
        refs["display_receipt_id"] = ack
    if mode == "different_tab":
        refs["client_view_id"] = "other-tab"
    if mode == "expired":
        c.clock.advance(1801)
    if refs:
        with pytest.raises(Conflict):
            turn(
                c,
                "自提",
                [intent("自提", "PROVIDE_INFO", candidate={"delivery": "pickup"})],
                **refs,
            )
    else:
        second = turn(
            c,
            "自提",
            [intent("自提", "PROVIDE_INFO", candidate={"delivery": "pickup"})],
            execute=False,
        )
        if mode == "late":
            show(c, first)
        c.runner.run(second)
        assert result(c, second) is None


def test_a50_alternative_selection_cannot_remove_original_condition(semantic_case):
    c = semantic_case
    text = "电池至少90%的话，900自提或者950包邮"
    options = [
        {
            **intent(
                "900自提",
                candidate=candidate(conditions=[battery_condition()]),
                alternative_group="a",
            ),
            "id": "a",
        },
        {
            **intent(
                "950包邮",
                candidate=candidate("950", "shipping", conditions=[battery_condition()]),
                alternative_group="a",
            ),
            "id": "b",
        },
    ]
    run = turn(c, text, options)
    assert result(c, run) is None
    card, ack = show(c, run)
    ambiguous = turn(
        c,
        "自提",
        [intent("自提", "PROVIDE_INFO", candidate={"delivery": "pickup"})],
        display_receipt_id=ack,
    )
    assert result(c, ambiguous) is None
    option = card["drafts"][0]
    chosen = turn(
        c,
        "继续",
        [intent("继续", "PROVIDE_INFO")],
        draft_id=option["draft_id"],
        draft_version=option["draft_version"],
    )
    assert result(c, chosen) is None
    with c.factory() as db:
        assert db.get(OfferDraft, option["draft_id"]).candidate["conditions"] == [
            battery_condition()
        ]


def test_a47_seller_fact_update_notifies_without_submitting(semantic_case):
    c = semantic_case
    text = "如果电池至少90%，900自提"
    first = turn(c, text, [intent(text, candidate=candidate(conditions=[battery_condition()]))])
    fact(c, "battery_health_pct", "95")
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).round_count == 0
        ready = db.scalar(
            select(Message).where(Message.session_id == c.sid, Message.role == "SYSTEM")
        )
        assert "尚未提交" in ready.content
        card = ready.card
    EvidenceMaintenance(c.factory, clock=c.clock).recheck(c.pid, c.seller)
    with c.factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.source_id.like("draft-ready:%"), Message.session_id == c.sid)
            )
            == 1
        )
    _, ack = show(c, first)
    run = turn(c, "继续", [intent("继续", "PROVIDE_INFO")], display_receipt_id=ack)
    assert result(c, run)["outcome"] == "OFFER_ACCEPTED"
    assert card["kind"] == "drafts"


@pytest.mark.parametrize("mode", ["no_rule", "overlap", "region_missing"])
def test_a48_unknown_cost_creates_actionable_verification(semantic_case, mode):
    c = semantic_case
    if mode == "overlap":
        rule(c)
        rule(c, "25")
    text = "900包邮到上海" if mode != "region_missing" else "900包邮"
    run = turn(
        c,
        text,
        [
            intent(
                text,
                candidate=candidate(
                    delivery="shipping", region="上海" if mode != "region_missing" else None
                ),
            )
        ],
    )
    assert result(c, run) is None
    data = EvidenceMaintenance(c.factory, clock=c.clock).inspect(c.pid, c.seller)
    if mode != "region_missing":
        assert data["verifications"][0]["status"] == "OPEN"
    else:
        assert "收货区域" in message(c, run).content


def test_a40_rule_changed_between_plan_and_commit_is_repriced(semantic_case):
    c = semantic_case
    data = rule(c, "20")
    rule_id = data["rules"][0]["id"]

    def update(point):
        if point == "after_plan":
            rule(c, "200", rule_id=rule_id)

    c.runner.fault = update
    text = "900包邮到上海"
    run = turn(c, text, [intent(text, candidate=candidate(delivery="shipping", region="上海"))])
    assert result(c, run)["outcome"] == "OFFER_REJECTED"
    assert "shipping_cost" not in str(message(c, run).card)


def test_a40_region_change_does_not_reuse_old_cost(semantic_case):
    c = semantic_case
    rule(c)
    text = "900包邮到上海"
    first = turn(c, text, [intent(text, candidate=candidate(delivery="shipping", region="上海"))])
    _, ack = show(c, first)
    changed = turn(
        c,
        "改寄北京",
        [intent("改寄北京", "CHANGE_TERMS", candidate={"region": "北京"})],
        display_receipt_id=ack,
    )
    assert result(c, changed) is None
    assert message(c, changed).card["drafts"][0]["candidate"]["region"] == "北京"


def test_confirm_rechecks_fact_and_cost_versions(semantic_case):
    c = semantic_case
    fact(c, "battery_health_pct", "95")
    text = "如果电池至少90%，900自提"
    first = turn(c, text, [intent(text, candidate=candidate(conditions=[battery_condition()]))])
    _, ack = show(c, first)
    fact(c, "battery_health_pct", "80")
    confirmation = turn(c, "行", [intent("行", "CONFIRM_INTENT")], display_receipt_id=ack)
    assert result(c, confirmation) is None
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).status.value == "ACTIVE"


def test_a61_refresh_same_offer_does_not_publish_or_extend_offer(semantic_case):
    c = semantic_case
    first = turn(c, "900自提")
    card, _ = show(c, first)
    with c.factory() as db:
        expiry = db.get(Offer, card["offer_id"]).expires_at
    late = turn(c, "行", [intent("行", "CONFIRM_INTENT")], execute=False)
    c.clock.advance(1801)
    refreshed = refresh_context(c.factory, c.sid, c.buyer, card["context_id"], "refresh", c.clock())
    assert refreshed == refresh_context(
        c.factory, c.sid, c.buyer, card["context_id"], "refresh", c.clock()
    )
    ack = NotificationService(c.factory, clock=c.clock).display(
        c.sid, c.buyer, refreshed["context_id"], "tab", "new-display"
    )
    c.runner.run(late)
    assert result(c, late) is None
    final = turn(c, "确认", [intent("确认", "CONFIRM_INTENT")], display_receipt_id=ack)
    assert result(c, final)["outcome"] == "INTENT_CONFIRMED"
    with c.factory() as db:
        assert db.get(Offer, card["offer_id"]).expires_at == expiry
        assert (
            db.scalar(
                select(func.count())
                .select_from(PublicInteractionContext)
                .where(PublicInteractionContext.message_id == message(c, first).id)
            )
            == 2
        )


@pytest.mark.parametrize(
    "point", ["after_semantics", "after_plan", "after_command", "after_publish", "after_end"]
)
def test_semantic_recovery_reuses_artifact_draft_and_action(semantic_case, point):
    c = semantic_case

    def crash(at):
        if at == point:
            raise RuntimeError("crash")

    c.runner.fault = crash
    run = turn(c, "900自提", execute=False)
    with pytest.raises(RuntimeError, match="crash"):
        c.runner.run(run)
    c.clock.advance()
    c.runner.fault = lambda at: None
    c.runner.run(run)
    assert c.model.calls == 1
    assert result(c, run)["outcome"] == "OFFER_ACCEPTED"
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).round_count == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(ModelCallLedger)
                .where(ModelCallLedger.input_id == run)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count()).select_from(OfferDraft).where(OfferDraft.input_id == run)
            )
            == 1
        )


def test_public_chat_and_display_api_are_owned_async_and_private(semantic_case):
    c = semantic_case
    app = create_app()
    app.dependency_overrides[get_session_factory_dependency] = lambda: c.factory
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=c.buyer)
    body = {"request_id": "unified-chat-001", "content": "900自提", "client_view_id": "tab"}
    with TestClient(app) as client:
        path = f"/api/negotiations/{c.sid}"
        response = client.post(path + "/messages", json=body)
        assert response.status_code == 202, response.text
        run = response.json()["run_id"]
        assert client.post(path + "/messages", json=body).json()["idempotent_replay"]
        assert (
            client.post(path + "/messages", json={**body, "client_view_id": "other"}).status_code
            == 409
        )
        assert c.model.calls == 0
        c.model.values[body["content"]] = {
            "intents": [intent(body["content"], candidate=candidate())]
        }
        c.runner.run(run)
        history = client.get(path + "/messages").json()["messages"]
        card = history[-1]["card"]
        detail = client.get(path)
        assert detail.status_code == 200, detail.text
        assert "shipping_cost" not in detail.text and "seller_borne_discount" not in detail.text
        ack = client.post(
            path + "/display-acks",
            json={
                "context_id": card["context_id"],
                "client_view_id": "tab",
                "request_id": "display",
            },
        )
        assert ack.status_code == 200
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="other")
        assert (
            client.post(
                path + "/display-acks",
                json={
                    "context_id": card["context_id"],
                    "client_view_id": "tab",
                    "request_id": "display",
                },
            ).status_code
            == 409
        )
        assert client.get(f"/api/seller/products/{c.pid}/evidence").status_code == 409


@pytest.mark.parametrize(
    "text,frame",
    [
        ("如果没修过900自提", {"intents": [intent("900自提", candidate=candidate())]}),
        ("不是900我说800自提", {"intents": [intent("不是900", candidate=candidate())]}),
        ("900自提或者800包邮", {"intents": [intent("900自提", candidate=candidate())]}),
        ("今天发货900自提", {"intents": [intent("900自提", candidate=candidate())]}),
        ("行但是得包邮", {"intents": [intent("行", "CONFIRM_INTENT")]}),
    ],
)
def test_unsafe_model_omissions_cannot_authorize(semantic_case, text, frame):
    c = semantic_case
    run = turn(c, text, frame["intents"])
    assert result(c, run) is None


def test_cyclic_or_ungrounded_model_outputs_clarify(semantic_case):
    c = semantic_case
    for bad in [
        intent("不存在", candidate=candidate()),
        intent("900自提", candidate=candidate(), depends_on=["i"]),
    ]:
        run = turn(c, "900自提", [bad])
        assert result(c, run) is None
    with pytest.raises(ValueError):
        IntentFrame.model_validate(
            {"intents": [intent("900自提", candidate={"amount": "900", "shipping_cost": "0"})]}
        )


def test_same_effective_proposal_does_not_add_rounds(semantic_case):
    c = semantic_case
    one = turn(c, "900自提")
    two = turn(c, "900自提可以吗", [intent("900自提可以吗", candidate=candidate("900.00"))])
    assert result(c, one)["offer_id"] == result(c, two)["offer_id"]
    assert result(c, two)["reused"]
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).round_count == 1


def test_missing_cost_after_plan_has_seller_verification(semantic_case):
    c = semantic_case
    data = rule(c)

    def update(point):
        if point == "after_plan":
            rule(c, rule_id=data["rules"][0]["id"], active=False)

    c.runner.fault = update
    text = "900包邮到上海"
    run = turn(c, text, [intent(text, candidate=candidate(delivery="shipping", region="上海"))])
    assert result(c, run) is None
    assert (
        EvidenceMaintenance(c.factory, clock=c.clock).inspect(c.pid, c.seller)["verifications"][0][
            "status"
        ]
        == "OPEN"
    )


def test_cost_supplement_requires_new_buyer_input(semantic_case):
    c = semantic_case
    text = "900包邮到上海"
    first = turn(c, text, [intent(text, candidate=candidate(delivery="shipping", region="上海"))])
    rule(c)
    assert result(c, first) is None
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).round_count == 0
    _, ack = show(c, first)
    resumed = turn(c, "继续", [intent("继续", "PROVIDE_INFO")], display_receipt_id=ack)
    assert result(c, resumed)["outcome"] == "OFFER_ACCEPTED"


@pytest.mark.parametrize("variant", ["missing_repair", "wrong_operator", "invented_delivery"])
def test_semantic_validator_rejects_partial_conditions_and_invented_terms(semantic_case, variant):
    c = semantic_case
    fact(c, "battery_health_pct", "95")
    if variant == "missing_repair":
        text = "如果电池至少90%而且没修过，900自提"
        proposal = candidate(conditions=[battery_condition()])
    elif variant == "wrong_operator":
        text = "如果电池至少90%，900自提"
        proposal = candidate(conditions=[{**battery_condition(), "operator": "<="}])
    else:
        text, proposal = "900可以吗", candidate()
    run = turn(c, text, [intent(text, candidate=proposal)])
    assert result(c, run) is None


def test_explicit_draft_reference_checks_owner_version_and_expiry(semantic_case):
    c = semantic_case
    first = turn(c, "900可以吗", [intent("900可以吗", candidate={"amount": "900"})])
    option = message(c, first).card["drafts"][0]
    for changes in ({"draft_version": 99}, {"draft_id": "unknown"}):
        with pytest.raises(Conflict):
            turn(
                c,
                "自提",
                [intent("自提", "PROVIDE_INFO", candidate={"delivery": "pickup"})],
                **{
                    **{"draft_id": option["draft_id"], "draft_version": option["draft_version"]},
                    **changes,
                },
            )
    c.clock.advance(86401)
    with pytest.raises(Conflict):
        turn(
            c,
            "继续",
            [intent("继续", "PROVIDE_INFO")],
            draft_id=option["draft_id"],
            draft_version=1,
        )


@pytest.mark.parametrize("point", ["after_claim", "after_semantics", "after_plan", "after_command"])
def test_semantic_actual_process_exit_and_replay(semantic_case, point):
    import json
    import subprocess
    import sys

    c = semantic_case
    run = turn(c, "900自提", execute=False)

    def process(fault=""):
        return subprocess.run(
            [sys.executable, "-m", "tests.graph.process_worker"],
            input=json.dumps(
                {
                    "run_id": run,
                    "now": c.clock().isoformat(),
                    "fault": fault,
                    "semantic_frames": c.model.values,
                }
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )

    killed = process(point)
    assert killed.returncode == 73, killed.stderr
    c.clock.advance(31)
    restored = process()
    assert restored.returncode == 0, restored.stderr
    assert result(c, run)["outcome"] == "OFFER_ACCEPTED"
    later = turn(c, "只是看看", [intent("只是看看", "PROVIDE_INFO")])
    assert result(c, later) is None
    assert c.runner.run(run) == json.loads(restored.stdout)
    with c.factory() as db:
        assert db.get(NegotiationSession, c.sid).round_count == 1


def test_button_confirmation_and_close_share_chat_fifo(semantic_case):
    c = semantic_case
    first = turn(c, "900自提")
    card, ack = show(c, first)
    changed = turn(
        c,
        "改成950自提",
        [intent("改成950自提", "CHANGE_TERMS", candidate=candidate("950"))],
        execute=False,
    )
    app = create_app()
    app.dependency_overrides[get_session_factory_dependency] = lambda: c.factory
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=c.buyer)
    with TestClient(app) as client:
        path = f"/api/negotiations/{c.sid}"
        confirmation = client.post(
            path + "/confirm",
            json={
                "request_id": "confirm-old-card",
                "offer_id": card["offer_id"],
                "client_view_id": "tab",
                "display_receipt_id": ack,
            },
        )
        assert confirmation.status_code == 202
        closing = client.post(
            path + "/close", json={"request_id": "close-in-queue", "client_view_id": "tab"}
        )
        assert closing.status_code == 202
    assert c.runner.run(confirmation.json()["run_id"]) is None
    c.runner.run(changed)
    c.runner.run(confirmation.json()["run_id"])
    assert result(c, confirmation.json()["run_id"]) is None
    c.runner.run(closing.json()["run_id"])
    assert result(c, closing.json()["run_id"])["outcome"] == "SESSION_CLOSED"


def test_seller_api_validates_fact_types_and_optimistic_version(semantic_case):
    c = semantic_case
    app = create_app()
    app.dependency_overrides[get_session_factory_dependency] = lambda: c.factory
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=c.seller)
    with TestClient(app) as client:
        path = f"/api/seller/products/{c.pid}"
        version = client.get(path + "/evidence").json()["product_version"]
        for value in ("101", "NaN", "90.001"):
            assert (
                client.put(
                    path + "/facts/battery_health_pct",
                    json={"expected_version": version, "value": value},
                ).status_code
                == 409
            )
        assert (
            client.put(
                path + "/facts/battery_health_pct",
                json={"expected_version": version, "value": "95"},
            ).status_code
            == 200
        )
        assert (
            client.put(
                path + "/facts/repair_history", json={"expected_version": version, "value": "NO"}
            ).status_code
            == 409
        )
