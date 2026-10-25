import json
import os

import pytest

from tests.graph.conftest import graph_factory  # noqa: F401


def test_p5_official_experiment(graph_factory):  # noqa: F811
    options = os.getenv("P5_EVALUATION_OPTIONS")
    if not options:
        pytest.skip("use scripts.run_p5_evaluation --allow-real-model")
    from evaluation.p5.runner import run

    report = run(graph_factory, os.environ["P0_POSTGRES_URL"], json.loads(options))
    # Execution success is explicitly distinct from the report's release gate.
    assert report["finished_at"]
    assert not any(r["status"] == "ERROR" for r in report["results"])
    assert "legacy_error" not in report
