from copy import deepcopy

import pytest
from sqlalchemy import select

from app.db.models import Message
from app.db.models.graph import BusinessActionReceipt, BusinessCausalEvent, NegotiationInput
from app.services.business_command_service import BusinessCommandService
from evaluation.p5.scoring import auxiliary_trajectory, causal_score
from tests.graph.test_business import submit


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
