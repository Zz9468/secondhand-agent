from copy import deepcopy

import pytest

from evaluation.p5.comparison_scope import audit


def cohorts():
    common = {
        "baseline_commit": "old",
        "prompt_sha256": "prompt",
        "evaluator_prompt_sha256": "judge",
        "dependency_lock_sha256": "lock",
        "packages": {"test": "1"},
        "model": "m",
        "model_endpoint_sha256": "endpoint",
        "temperature": 0,
        "enable_thinking": False,
        "sdk_retries": 0,
        "runtime_budget": {"calls": 10, "tokens": 180000},
        "shared_model_config": {"max_tokens": 2048},
        "auxiliary_model_config": {},
        "working_tree_dirty": False,
        "runtime_files_sha256": {"backend/app/model.py": "same"},
    }
    cases = [
        {
            "case_id": str(i),
            "version": "v1",
            "group": "business_preserving" if i < 79 else "behavior_change",
            "product": {"listed_price": "3000"},
        }
        for i in range(111)
    ]
    after = [
        {
            **deepcopy(c),
            "version": "v2",
            "group": "business_preserving",
            "comparison_basis": "static",
        }
        for c in cases
    ]
    config = {
        "model": "m",
        "max_tokens": 2048,
        "temperature": 0,
        "enable_thinking": False,
        "sdk_retries": 0,
        "timeout_seconds": 45,
    }

    def rows(ids, old=False):
        return [
            {
                "case_id": str(i),
                "repeat": n,
                "status": "OK",
                "business_pass": True,
                "database_evidence": True,
                "runs": [],
                "recorded_calls": [
                    {"model_config": deepcopy(config), "tokens": 1, "estimated_cny": "0"}
                ]
                if old
                else [],
            }
            for i in ids
            for n in range(3)
        ]

    base = {
        "finished_at": "done",
        "frozen_runtime": {"status": "PASS"},
        "model_configuration_comparison": {"status": "PASS"},
    }
    primary = {
        **deepcopy(base),
        "metadata": {**deepcopy(common), "source_commit": "a"},
        "cases": cases,
        "results": rows(range(111)),
        "legacy_results": rows(range(79), True),
        "options": {"repetitions": 3, "smoke": True},
    }
    extension = {
        **deepcopy(base),
        "metadata": {**deepcopy(common), "source_commit": "b"},
        "cases": after,
        "results": rows(range(79, 111)),
        "legacy_results": rows(range(79, 111), True),
        "options": {"repetitions": 3, "select": [str(i) for i in range(79, 111)]},
    }
    return primary, extension


def test_disjoint_comparison_keeps_primary_new_observations_and_all_failures():
    a, b = cohorts()
    originals = deepcopy((a, b))
    result = audit(a, b)
    assert result["status"] == "PASS" and result["old_business"]["runs"] == 333
    assert (a, b) == originals
    b["legacy_results"][0].update(status="ERROR", business_pass=False)
    result = audit(a, b)
    assert result["old_business"]["runs"] == 333 and result["old_business"]["execution_errors"] == 1
    b["results"][0]["business_pass"] = False
    assert audit(a, b)["status"] == "FAIL"


@pytest.mark.parametrize("mutation", ["facts", "code", "model", "duplicate", "missing_primary"])
def test_scope_join_rejects_incompatible_or_incomplete_cohorts(mutation):
    a, b = cohorts()
    if mutation == "facts":
        b["cases"][0]["product"]["listed_price"] = "1"
    elif mutation == "code":
        b["metadata"]["runtime_files_sha256"]["backend/app/model.py"] = "changed"
    elif mutation == "model":
        b["metadata"]["model"] = "other"
    elif mutation == "duplicate":
        b["legacy_results"].append(deepcopy(b["legacy_results"][0]))
    else:
        a["results"].pop()
    with pytest.raises(AssertionError):
        audit(a, b)
