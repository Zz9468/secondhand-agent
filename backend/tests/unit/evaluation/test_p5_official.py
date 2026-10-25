import asyncio
from types import SimpleNamespace

from evaluation.p5.datasets import load_cases
from evaluation.p5.official import feedback_complete, readback, safe_result
from evaluation.p5.runner import comparison


def test_export_allowlist_excludes_private_payloads_and_credentials():
    row = {
        "status": "OK",
        "business_pass": True,
        "target": "graph",
        "policy": {"minimum_net_price": "SYNTHETIC_PRIVATE_MARKER"},
        "display_receipt_id": "SYNTHETIC_PRIVATE_MARKER",
        "runs": [],
        "judge": {"score": True, "reasoning": "SYNTHETIC_PRIVATE_MARKER"},
    }
    value = safe_result(row)
    assert "SYNTHETIC_PRIVATE_MARKER" not in str(value)
    assert value["quality_score"] is True


def test_missing_or_failed_remote_feedback_never_completes():
    assert not feedback_complete(SimpleNamespace(feedback_stats=None))
    assert not feedback_complete(
        SimpleNamespace(
            feedback_stats={
                "business_correct": {"n": 1},
                "public_quality": {"n": 1, "errors": 1},
            }
        )
    )


def test_official_readback_checks_ids_and_feedback():
    row = SimpleNamespace(
        id="run",
        feedback_stats={
            "business_correct": {"n": 1},
            "public_quality": {"n": 1},
        },
    )

    class Runs:
        async def query(self, **kwargs):
            assert kwargs["project_ids"] == ["project"]
            assert "FEEDBACK_STATS" in kwargs["selects"]
            yield row

    assert asyncio.run(readback(SimpleNamespace(runs=Runs()), "project", {"run"})) == [row]


def test_frozen_risk_candidates_are_not_certified_human_iid_holdouts():
    cases = load_cases()
    risk = [c for c in cases if c.get("risk_class")]
    assert len(risk) == 24
    assert all(c["split"] == "holdout_candidate" and not c["human_labelled"] for c in risk)
    assert len([c for c in cases if c.get("smoke")]) == 200


def test_comparison_missing_or_regressing_results_fail_closed():
    cases = [{"case_id": "a"}]
    good = [{"case_id": "a", "business_pass": True}]
    bad = [{"case_id": "a", "business_pass": False}]
    assert comparison(cases, good, [])["status"] == "INSUFFICIENT_EVIDENCE"
    assert comparison(cases, bad, good)["status"] == "FAIL"
