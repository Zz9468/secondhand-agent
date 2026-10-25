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
