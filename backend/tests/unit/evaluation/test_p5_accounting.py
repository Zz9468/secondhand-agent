import json

import pytest

from evaluation.p5.accounting import collect


def test_cost_ledger_preserves_unknown_and_rejects_duplicate_reassessment(tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "stage_usage": {
                    "recorded_seller_calls": 5,
                    "recorded_auxiliary_calls": 2,
                    "known_tokens": 50,
                    "known_estimated_cny": "0.10",
                    "unknown_usage_calls": 1,
                    "unknown_cost_calls": 1,
                    "early_probe_legacy_rpc_count": None,
                }
            }
        )
    )
    row = {
        "status": "OK",
        "database_evidence": True,
        "runs": [
            {
                "run_id": "unique",
                "calls": [
                    {"tokens": 10, "estimated_cny": "0.02"},
                    {"tokens": None, "estimated_cny": None},
                ],
            }
        ],
    }
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"results": [row]}))
    result = collect(baseline, [report], [])["stage_usage"]
    assert result["recorded_seller_calls"] == 7 and result["known_tokens"] == 60
    assert result["known_estimated_cny"] == "0.12" and result["unknown_cost_calls"] == 2
    assert result["early_probe_legacy_rpc_count"] is None
    with pytest.raises(ValueError, match="duplicate"):
        collect(baseline, [report, report], [])
    reassessed = tmp_path / "reassessed.json"
    reassessed.write_text(json.dumps({"results": [{**row, "judge": {"score": True}}]}))
    with pytest.raises(ValueError, match="overlaps"):
        collect(baseline, [report, reassessed], [])
