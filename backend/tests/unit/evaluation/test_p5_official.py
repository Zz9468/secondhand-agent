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


def test_simulator_keeps_pending_conditions_but_no_draft_identifiers():
    reply = public_reply(
        SimpleNamespace(
            content="待澄清",
            card={
                "kind": "drafts",
                "context_id": "private-context",
                "drafts": [
                    {
                        "draft_id": "private-draft",
                        "draft_version": 78,
                        "candidate": {"amount": "900", "conditions": [{"value": "98"}]},
                        "missing": ["condition:battery_health_pct:UNSATISFIED"],
                    }
                ],
            },
        )
    )
    assert "900" in reply and "98" in reply and "UNSATISFIED" in reply
    assert "private-context" not in reply and "private-draft" not in reply
    assert "draft_version" not in reply


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


def test_long_history_prefix_is_enforced_without_model_authorization():
    from evaluation.p5.official import consultation_prefix

    for case in load_cases():
        if case["case_id"].startswith("release120-v4_long_history"):
            prefix = consultation_prefix(case)
            assert prefix[: len(case["turns"])] == case["turns"]
            assert len(prefix) == case["buyer_profile"]["min_consultation_turns"] == 12


def test_windows_reader_cannot_abort_durable_report_on_transient_replace(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from evaluation.p5.runner import save_report

    replace = Path.replace
    attempts = []

    def transient(path, target):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("synthetic Windows reader")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", transient)
    save_report(tmp_path, {"results": [{"status": "ERROR"}]})
    assert len(attempts) == 3
    assert json.loads((tmp_path / "report.json").read_text())["results"][0]["status"] == "ERROR"


def test_previous_answer_cannot_mask_an_omitted_current_question():
    from evaluation.p5.target import score_case

    case = {
        "case_id": "per-turn-answer",
        "expected_status": "ACTIVE",
        "required_replies": {"1": ["900"]},
    }
    snapshot = {
        "session_status": "ACTIVE",
        "offers": [],
        "runs": [
            {"receipt": None, "message": {"id": 1}, "reply": "报价900元"},
            {"receipt": None, "message": {"id": 2}, "reply": "页面标价1000元"},
        ],
    }
    assert "intent_coverage_turn_1" in score_case(case, snapshot)["failures"]
    snapshot["runs"][1]["reply"] = "已展示报价900元，尚待独立确认"
    assert score_case(case, snapshot)["business_pass"]
