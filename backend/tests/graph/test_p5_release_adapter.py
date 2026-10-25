import os

import pytest

from evaluation.p5.datasets import load_cases
from evaluation.p5.official import simulate
from evaluation.p5.target import GraphTarget, score_case


@pytest.mark.parametrize(
    "spec", [c for c in load_cases() if c.get("graph_entry") == "typed"], ids=lambda c: c["case_id"]
)
def test_release_structured_business_uses_real_graph_and_databases(graph_factory, spec):
    target = GraphTarget(graph_factory, os.environ["P0_POSTGRES_URL"], spec)
    simulate(target, spec, [])
    snapshot = target.snapshot()
    scored = score_case(spec, snapshot)
    assert scored["business_pass"], scored["failures"]
    assert all(r["graph_version"] == "negotiation-v1" for r in snapshot["runs"])


def test_question_does_not_discard_previous_real_display_receipt(graph_factory):
    from app.runtime.graph_runner import GraphRunner
    from tests.graph.test_p4_quality import FakeQuality
    from tests.graph.test_semantic import FakeSemanticModel, candidate, intent

    case = next(c for c in load_cases() if c["case_id"] == "quality-two-questions")
    target = GraphTarget(graph_factory, os.environ["P0_POSTGRES_URL"], case)
    model = FakeSemanticModel()
    target.runner = GraphRunner(
        graph_factory,
        os.environ["P0_POSTGRES_URL"],
        queue=target.queue,
        semantic_provider=model,
        quality_provider=FakeQuality(),
    )
    for text, frame in [
        ("900自提", intent("900自提", candidate=candidate())),
        ("标价多少？", intent("标价多少？", kind="ASK_PRODUCT", topic="listed_price")),
        ("确认", intent("确认", kind="CONFIRM_INTENT")),
    ]:
        model.values[text] = {"intents": [frame]}
        target({"content": text}, thread_id=target.thread_id)
        if text == "900自提":
            receipt = target.ack
        elif text == "标价多少？":
            assert target.ack == receipt
    snapshot = target.snapshot()
    assert snapshot["session_status"] == "AGREED"
    assert snapshot["runs"][-1]["causal_score"]["status"] == "PASS"
