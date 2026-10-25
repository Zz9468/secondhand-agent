import hashlib
import json

from app.runtime.contracts import digest
from evaluation.p5.datasets import DATA, load_cases
from evaluation.p5.import_release import adapt
from evaluation.p5.references import public_fixture_reference


def test_complete_lossless_release_mapping_and_frozen_sources():
    root = DATA.parent / "p5_sources" / "release120"
    source = json.loads((root / "v4_release_compatible.json").read_text(encoding="utf-8"))
    profiles = json.loads((root / "v4_release_profiles.json").read_text(encoding="utf-8"))
    cases = [c for c in load_cases() if c.get("source_scenario")]
    assert len(cases) == len(source["scenarios"]) == 120
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob("*.json")}
    for case, original in zip(cases, source["scenarios"], strict=True):
        assert case == adapt(original, profiles, hashes)
        assert case["source_sha256"] == digest(original)
        assert case["source_scenario"] == original
        assert public_fixture_reference(case)["listed_price"] == "3000.00"
        assert not case["human_labelled"] and case["used_for_tuning"]
    assert sum(c["graph_entry"] == "typed" for c in cases) == 68
    assert sum(c["dynamic"] for c in cases) == 20
    assert sum(c["buyer_profile"].get("min_consultation_turns", 0) == 12 for c in cases) == 4


def test_compatible_unknown_shipping_openings_have_no_formal_offer():
    cases = {c["case_id"]: c for c in load_cases()}
    for suffix in ("terms_11_unknown_shipping", "terms_12_unknown_shipping_with_term"):
        case = cases["release120-" + suffix]
        assert case["scripted_offers"] == [None]
        assert case["expected_outcomes"] == ["NO_ACTION"]
    assert cases["risk-condition-0"]["split"] == "development_regression"


def test_long_history_requires_consultation_not_just_twelve_actions():
    from evaluation.p5.release_scoring import score_release

    case = next(c for c in load_cases() if c["case_id"] == "release120-v4_long_history_01")
    snapshot = {"session_status": "ACTIVE", "offers": [], "runs": [{} for _ in range(12)]}
    assert score_release(case, snapshot) == []
    snapshot["runs"][3] = {"receipt": {"outcome": "OFFER_ACCEPTED"}}
    assert "long_history_consultation_prefix_executed_action" in score_release(case, snapshot)
