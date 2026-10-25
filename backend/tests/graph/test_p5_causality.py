from copy import deepcopy

import pytest
from sqlalchemy import select

from app.db.models import Message
from app.db.models.graph import BusinessActionReceipt, BusinessCausalEvent, NegotiationInput
from app.services.business_command_service import BusinessCommandService
from evaluation.p5.scoring import auxiliary_trajectory, causal_score
from tests.graph.test_business import submit
from tests.graph.test_p4_quality import quality_case  # noqa: F401
from tests.graph.test_semantic import semantic_case  # noqa: F401


def read_chain(factory, run_id):
    with factory() as db:
        run = db.get(NegotiationInput, run_id)
        receipt = db.get(BusinessActionReceipt, f"turn:{run_id}:action")
        events = list(
            db.scalars(
                select(BusinessCausalEvent).where(BusinessCausalEvent.action_id == receipt.id)
            )
        )
        message = db.scalar(select(Message).where(Message.source_id == f"turn:{run_id}:reply"))
        return (
            [
                {
                    k: getattr(e, k)
                    for k in ("id", "action_id", "sequence", "stage", "parent_id", "evidence")
                }
                for e in events
            ],
            {
                "input_seq": run.input_seq,
                "session_id": run.session_id,
                **{
                    k: getattr(receipt, k)
                    for k in ("id", "input_id", "before_version", "after_version", "result")
                },
            },
            {k: getattr(message, k) for k in ("id", "session_id", "source_id", "formal_offer_id")}
            if message
            else None,
        )


def test_a58_actual_database_chain_and_idempotent_replay(case):
    factory, sid, buyer, _, queue, runner = case
    run = queue.enqueue(sid, buyer, "p5", submit())
    assert runner.run(run)
    assert runner.run(run)
    evidence = read_chain(factory, run)
    assert causal_score(*evidence)["status"] == "PASS"
    assert len(evidence[0]) == 4
    broken = deepcopy(evidence)
    broken[0][1]["evidence"]["conditions_hash"] = "different"
    assert auxiliary_trajectory(broken[0])["score"] is True
    assert causal_score(*broken)["status"] == "FAIL"


@pytest.mark.parametrize("point", ["authorization", "offer", "receipt", "notification", "outbox"])
def test_audit_rolls_back_with_business_transaction(case, point):
    factory, sid, buyer, _, queue, _ = case
    run = queue.enqueue(sid, buyer, "rollback", submit("800"))
    owner = queue.claim(run)

    def fault(at):
        if at == point:
            raise RuntimeError("audit write gap")

    with pytest.raises(RuntimeError, match="audit write gap"):
        BusinessCommandService(factory, clock=queue.clock, fault=fault).execute(run, owner, 1)
    with factory() as db:
        assert not db.get(BusinessActionReceipt, f"turn:{run}:action")
        assert not db.scalar(
            select(BusinessCausalEvent).where(BusinessCausalEvent.action_id == f"turn:{run}:action")
        )


def test_official_adapter_replays_identical_payload_after_display(quality_case):  # noqa: F811
    from evaluation.p5.target import GraphTarget
    from tests.graph.test_semantic import candidate, intent

    c = quality_case
    text = "900自提可以吗？"
    c.model.values[text] = {"intents": [intent(text, candidate=candidate())]}
    target = GraphTarget.__new__(GraphTarget)
    target.factory, target.sid, target.buyer = c.factory, c.sid, c.buyer
    target.queue, target.runner = c.queue, c.runner
    target.case = {"mode": "replay", "show_cards": True}
    target.run_ids, target.latencies, target.outputs = [], [], []
    target.ack = target.card = target.trace_client = None
    target.thread_id = str(c.sid)
    # Fixture uses a fixed clock; display and enqueue need the same clock domain.
    from app.runtime.contracts import utcnow

    c.queue.clock = utcnow
    first = target({"content": text}, thread_id=target.thread_id)
    second = target({"content": text}, thread_id=target.thread_id)
    assert first == second and target.run_ids[0] == target.run_ids[1]
    assert len(read_chain(c.factory, target.run_ids[0])[0]) == 4


@pytest.mark.parametrize("operator", ["<=", "==", None, ">="])
def test_failed_risk_case_negative_battery_condition_never_accepts_92(
    semantic_case,  # noqa: F811
    operator,
):
    from app.db.models import Offer
    from tests.graph.test_semantic import candidate, fact, intent, turn

    c = semantic_case
    fact(c, "battery_health_pct", 92)
    text = "电池不到98%我就不买，满足这个前提我出900自提。"
    conditions = (
        [
            {
                "fact": "battery_health_pct",
                "operator": operator,
                "value": "98",
                "source_quote": "电池不到98%",
            }
        ]
        if operator
        else []
    )
    run = turn(c, text, [intent(text, candidate=candidate(conditions=conditions))])
    with c.factory() as db:
        assert not db.get(BusinessActionReceipt, f"turn:{run}:action")
        assert not db.scalar(select(Offer).where(Offer.session_id == c.sid))


def test_negative_floor_with_correct_predicate_can_execute_when_fact_satisfies(semantic_case):  # noqa: F811
    from tests.graph.test_semantic import candidate, fact, intent, result, turn

    c = semantic_case
    fact(c, "battery_health_pct", 99)
    text = "电池低于98%就不买，900自提。"
    conditions = [
        {
            "fact": "battery_health_pct",
            "operator": ">=",
            "value": "98.0",
            "source_quote": "电池低于98%",
        }
    ]
    run = turn(c, text, [intent(text, candidate=candidate(conditions=conditions))])
    assert result(c, run)["outcome"] == "OFFER_ACCEPTED"
