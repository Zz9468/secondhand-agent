import asyncio
from types import SimpleNamespace

from evaluation.p5.datasets import load_cases
from evaluation.p5.official import feedback_complete, readback, safe_result
from evaluation.p5.runner import comparison
from evaluation.p5.target import public_reply


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


def test_simulator_sees_public_card_terms_without_confirmation_credentials():
    reply = public_reply(
        SimpleNamespace(
            content="报价已接受",
            card={
                "amount": "900.00",
                "terms": {"delivery_method": "pickup"},
                "context_id": "private-display-context",
                "offer_id": 54321,
            },
        )
    )
    assert "900.00" in reply and "pickup" in reply
    assert "private-display-context" not in reply and "54321" not in reply


def test_missing_or_failed_remote_feedback_never_completes():
    assert not feedback_complete(SimpleNamespace(feedback_stats=None))
    assert not feedback_complete(SimpleNamespace(feedback_stats={"business_correct": None}))
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
        end_time="complete",
        error=None,
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
    assert all(
        c["split"] in {"holdout_candidate", "development_regression"} and not c["human_labelled"]
        for c in risk
    )
    assert next(c for c in risk if c["case_id"] == "risk-condition-0")["used_for_tuning"]
    assert len([c for c in cases if c.get("smoke")]) == 200


def test_comparison_missing_or_regressing_results_fail_closed():
    cases = [{"case_id": "a"}]
    good = [{"case_id": "a", "business_pass": True}]
    bad = [{"case_id": "a", "business_pass": False}]
    assert comparison(cases, good, [])["status"] == "INSUFFICIENT_EVIDENCE"
    assert comparison(cases, bad, good)["status"] == "FAIL"
