import json

from app.runtime.contracts import digest
from evaluation.p5 import reassess as module
from evaluation.p5.datasets import load_cases
from evaluation.p5.runner import sha


def test_reassessment_preserves_all_attempts_errors_and_original_evidence(tmp_path, monkeypatch):
    cases = load_cases()
    base = {
        "repeat": 0,
        "status": "OK",
        "judge": {"score": False},
        "auxiliary_calls": [],
        "runs": [],
        "trajectory": [],
    }
    new = {
        **base,
        "case_id": "risk-condition-0",
        "target": "graph",
        "business_pass": False,
        "risk_error": True,
    }
    smoke = {**base, "case_id": "smoke-000", "target": "graph", "business_pass": True}
    old = {
        **base,
        "case_id": "compare-price-749",
        "target": "legacy",
        "business_pass": False,
        "database_evidence": True,
        "session_status": "ACTIVE",
        "tasks": [],
        "runs": [{"outcome": "REJECTED"}],
        "messages": [],
    }
    error = {**old, "repeat": 1, "status": "ERROR", "error_type": "OperationalError"}
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "metadata": {"source_commit": "seller-commit", "dataset_sha256": digest(cases)},
                "results": [new, smoke],
                "legacy_results": [old, error],
                "remote": [{"status": "old-result"}],
            }
        ),
        encoding="utf-8",
    )
    before = sha(source)
    calls = []

    def judge(case, trajectory, usage):
        calls.append(case["case_id"])
        usage.append({"tokens": 10, "estimated_cny": "0.1"})
        return {"score": True}

    monkeypatch.setattr(module, "judge", judge)
    monkeypatch.setattr(module, "export_experiment", lambda *a, **kw: {"status": "PASS"})
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **kw: "evaluator-commit")
    report = module.reassess(source, tmp_path / "derived")
    assert sha(source) == before == report["reassessment"]["source_sha256"]
    assert report["metadata"]["source_commit"] == "seller-commit"
    assert report["metadata"]["evaluator_commit"] == "evaluator-commit"
    assert len(calls) == 3 and "smoke-000" not in calls
    assert report["results"][0]["risk_error"] is True
    assert report["results"][0]["business_pass"] is False
    assert report["results"][0]["previous_judges"] == [{"score": False}]
    assert report["legacy_results"][0]["business_pass"] is True
    assert report["legacy_results"][0]["original_business_pass"] is False
    assert report["legacy_results"][1]["business_pass"] is False
    assert report["legacy_results"][1]["error_type"] == "OperationalError"
    assert len(report["remote"]) == 3
