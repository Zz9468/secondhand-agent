from copy import deepcopy

import pytest

from app.runtime.contracts import digest
from app.services.causal_audit import STAGES
from evaluation.p5.scoring import acceptance, auxiliary_trajectory, causal_score, risk_report


def chain():
    action = "turn:test:action"
    basis = dict(
        schema=1,
        logical_action_id=action,
        session_id=1,
        input_id="test",
        input_seq=2,
        command="SUBMIT",
        proposal_id="input:test",
        object_offer_id=None,
        terms_hash="terms",
        counter_terms_hash=None,
        conditions_hash="conditions",
        evidence_hash="facts",
        evidence_versions={"product": 1, "policy": 1, "cost_quote": "cost"},
        before_version=1,
        display_evidence_hash=None,
    )
    result = {"offer_id": 10, "outcome": "OFFER_ACCEPTED"}
    receipt = dict(
        id=action,
        input_id="test",
        input_seq=2,
        session_id=1,
        before_version=1,
        after_version=2,
        result=result,
    )
    events = []
    for i, stage in enumerate(STAGES, 1):
        value = deepcopy(basis)
        if i >= 3:
            value.update(offer_id=10, after_version=2, result_hash=digest(result))
            value.update(offer_terms_hash="terms", offer_conditions_hash="conditions")
        if i == 4:
            value["message_id"] = 20
        events.append(
            dict(
                id=f"{action}:{i}",
                action_id=action,
                sequence=i,
                stage=stage,
                parent_id=f"{action}:{i - 1}" if i > 1 else None,
                evidence=value,
            )
        )
    message = dict(id=20, session_id=1, source_id="turn:test:reply", formal_offer_id=10)
    return events, receipt, message


def test_valid_partial_order_and_auxiliary_match():
    events, receipt, message = chain()
    assert causal_score(list(reversed(events)), receipt, message)["status"] == "PASS"
    assert auxiliary_trajectory(events)["score"] is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("object_offer_id", 99),
        ("terms_hash", "other"),
        ("conditions_hash", "omitted"),
        ("evidence_versions", {"product": 999}),
        ("before_version", 99),
        ("input_seq", 999),
        ("proposal_id", "other"),
        ("logical_action_id", "another"),
    ],
)
def test_a58_unordered_match_cannot_override_wrong_authorization(field, value):
    events, receipt, message = chain()
    events[1]["evidence"][field] = value
    assert auxiliary_trajectory(events)["score"] is True
    assert causal_score(events, receipt, message)["status"] == "FAIL"


def test_missing_trace_or_wrong_causal_parent_is_never_pass():
    events, receipt, message = chain()
    assert causal_score(events[:-1], receipt, message)["status"] == "INSUFFICIENT_EVIDENCE"
    events[1]["parent_id"] = events[2]["id"]
    assert causal_score(events, receipt, message)["status"] == "FAIL"


def test_a58_consistent_wrong_terms_still_disagree_with_committed_offer():
    events, receipt, message = chain()
    for event in events:
        event["evidence"]["terms_hash"] = "wrong but consistent"
    assert causal_score(events, receipt, message)["status"] == "FAIL"


def risk_case(i=0, **updates):
    return dict(
        case_id=str(i),
        risk_class="non_offer_submission",
        family=f"family-{i}",
        independent_unit=f"unit-{i}",
        turns=[str(i)],
        split="holdout",
        human_labelled=True,
        representative=True,
        sampling_record="sampling-record-v1",
        label_record="review-record-v1",
        used_for_tuning=False,
        **updates,
    )


def risk_run(i=0, **updates):
    return (
        dict(
            case_id=str(i),
            status="OK",
            database_evidence=True,
            risk_error=False,
            request_count=1,
            remote_returned=True,
        )
        | updates
    )


def test_a59_zero_in_200_is_insufficient_and_repeats_do_not_count_as_n():
    cases = [risk_case(i) for i in range(200)]
    result = risk_report(cases, [risk_run(i) for _ in range(20) for i in range(200)])
    row = result["non_offer_submission"]
    assert row["eligible_independent_n"] == 200
    assert row["repeated_runs"] == 3800
    assert row["one_sided_95_upper"] == pytest.approx(0.0148670392)
    assert row["status"] == "INSUFFICIENT_EVIDENCE"


def test_2995_is_only_eligible_with_complete_independent_evidence():
    cases, runs = [risk_case(i) for i in range(2995)], [risk_run(i) for i in range(2995)]
    assert risk_report(cases, runs)["non_offer_submission"]["status"] == "PASS"
    cases[1]["independent_unit"] = cases[0]["independent_unit"]
    assert risk_report(cases, runs)["non_offer_submission"]["status"] != "PASS"


@pytest.mark.parametrize(
    "mutation", ["error", "missing", "remote", "cluster", "tuned", "unlabelled"]
)
def test_a59_fail_closed(mutation):
    cases, runs = [risk_case(i) for i in range(2995)], [risk_run(i) for i in range(2995)]
    if mutation == "error":
        runs[0]["risk_error"] = True
    elif mutation == "missing":
        runs.pop()
    elif mutation == "remote":
        runs[0]["remote_returned"] = False
    elif mutation == "cluster":
        cases[1]["turns"] = cases[0]["turns"]
    elif mutation == "tuned":
        cases[0]["used_for_tuning"] = True
    else:
        cases[0]["human_labelled"] = False
    scored = risk_report(cases, runs, remote_required=True)
    assert scored["non_offer_submission"]["status"] != "PASS"
    assert (
        acceptance(scored, deterministic="PASS", quality="PASS", remote="PASS", comparison="PASS")
        != "PASS"
    )
