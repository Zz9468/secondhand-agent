from copy import deepcopy

from evaluation.p5.datasets import load_cases
from evaluation.p5.legacy_scoring import score_legacy
from evaluation.p5.references import public_fixture_reference


def test_old_rejection_alias_is_success_but_wrong_action_or_error_is_not():
    case = {"expected_status": "ACTIVE", "expected_outcomes": ["OFFER_REJECTED"]}
    row = {
        "status": "OK",
        "database_evidence": True,
        "session_status": "ACTIVE",
        "tasks": [{"status": "SUCCEEDED"}],
        "runs": [{"outcome": "REJECTED"}],
    }
    assert score_legacy(case, row)
    for update in [
        {"status": "ERROR"},
        {"database_evidence": False},
        {"tasks": [{"status": "FAILED"}]},
        {"session_status": "AGREED"},
        {"runs": [{"outcome": "OFFER_ACCEPTED"}]},
        {"runs": []},
    ]:
        assert not score_legacy(case, {**deepcopy(row), **update})


def test_judge_reference_includes_actual_public_fixture_without_private_thresholds():
    case = load_cases()[0]
    facts = public_fixture_reference(case)
    assert facts["listed_price"] == "1000"
    assert "维修情况未知" in facts["seller_description"]
    assert facts["verified_facts"]["allowed_delivery_methods"] == ["pickup", "shipping"]
    assert "750" not in str(facts) and "850" not in str(facts)
