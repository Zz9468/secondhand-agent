from evaluation.p5.risk_corpus import candidates, contamination
from evaluation.p5.scoring import risk_report


def test_ai_candidate_claims_cannot_establish_human_or_distribution_evidence():
    cases = candidates()
    assert len(cases) == 18 and len({c["case_id"] for c in cases}) == 18
    for row in risk_report(cases, []).values():
        assert row["eligible_independent_n"] == 0
        assert row["status"] == "INSUFFICIENT_EVIDENCE"


def test_contamination_detects_amount_rewrites_and_family_overlap():
    candidate = dict(case_id="reserve", turns=["我预算1900，不报价"], family="budget")
    development = [dict(case_id="dev", turns=["我预算900，不报价"], family="other")]
    assert contamination([candidate], development)[0]["status"] == "QUARANTINE"
    development[0].update(turns=["unrelated wording"], family="budget")
    assert contamination([candidate], development)[0]["overlaps"][0]["case_id"] == "dev"
