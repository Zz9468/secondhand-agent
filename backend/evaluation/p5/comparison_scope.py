"""Verify two frozen cohorts before joining disjoint legacy comparison cases."""

import hashlib
import json
import sys
from pathlib import Path

from evaluation.p5.legacy_config import configuration_audit
from evaluation.p5.runner import comparison, summarize


def audit(primary, extension):
    assert primary.get("finished_at") and extension.get("finished_at")
    for r in (primary, extension):
        assert r["frozen_runtime"]["status"] == "PASS"
        assert r["metadata"]["working_tree_dirty"] is False
    a, b = primary["metadata"], extension["metadata"]

    def apps(m):
        return {k: v for k, v in m["runtime_files_sha256"].items() if k.startswith("backend/app/")}

    assert apps(a) == apps(b)
    same = (
        "baseline_commit",
        "prompt_sha256",
        "evaluator_prompt_sha256",
        "dependency_lock_sha256",
        "packages",
        "model",
        "model_endpoint_sha256",
        "temperature",
        "enable_thinking",
        "sdk_retries",
        "runtime_budget",
        "shared_model_config",
        "auxiliary_model_config",
    )
    assert all(a[k] == b[k] for k in same)
    before, after = ({c["case_id"]: c for c in r["cases"]} for r in (primary, extension))

    def strip(c):
        return {k: v for k, v in c.items() if k not in {"version", "group", "comparison_basis"}}

    assert before.keys() == after.keys()
    assert all(strip(before[k]) == strip(after[k]) for k in before)
    original_ids = {k for k, c in before.items() if c["group"] == "business_preserving"}
    current_ids = {k for k, c in after.items() if c["group"] == "business_preserving"}
    additional = current_ids - original_ids
    assert len(original_ids) == 79 and len(current_ids) == 111 and len(additional) == 32
    assert original_ids <= current_ids
    assert set(extension["options"]["select"]) == additional
    assert primary["options"]["repetitions"] == extension["options"]["repetitions"] == 3
    primary_expected = {
        (c["case_id"], i)
        for c in primary["cases"]
        if (not primary["options"].get("select") or c["case_id"] in primary["options"]["select"])
        and (primary["options"].get("smoke") or not c.get("smoke"))
        for i in range(1 if c.get("smoke") else 3)
    }
    primary_keys = [(r["case_id"], r["repeat"]) for r in primary["results"]]
    assert len(primary_keys) == len(set(primary_keys)) and set(primary_keys) == primary_expected
    for rows, ids in (
        (extension["results"], additional),
        (extension["legacy_results"], additional),
        (primary["legacy_results"], original_ids),
    ):
        keys = [(r["case_id"], r["repeat"]) for r in rows]
        assert len(keys) == len(set(keys)) and set(keys) == {(k, i) for k in ids for i in range(3)}
    old = primary["legacy_results"] + extension["legacy_results"]
    common = [after[k] for k in sorted(current_ids)]
    joined = comparison(common, primary["results"], old)
    model = configuration_audit(old, a["model"])
    statuses = [
        joined["status"],
        model["status"],
        primary["model_configuration_comparison"]["status"],
        extension["model_configuration_comparison"]["status"],
    ]
    if any(
        not r.get("business_pass") or r["status"] != "OK"
        for r in primary["results"] + extension["results"]
    ):
        statuses.append("FAIL")
    status = (
        "FAIL"
        if "FAIL" in statuses
        else "PASS"
        if all(s == "PASS" for s in statuses)
        else "INSUFFICIENT_EVIDENCE"
    )
    return {
        "status": status,
        "primary_source_commit": a["source_commit"],
        "extension_source_commit": b["source_commit"],
        "same_production_app_files": True,
        "same_prompts_dependencies_models_and_budgets": True,
        "identical_case_semantics": True,
        "only_changed_case_fields": ["version", "group", "comparison_basis"],
        "primary_new_runs": len(primary["results"]),
        "additional_new_runs": len(extension["results"]),
        "comparable_cases": 111,
        "new_comparable_observations": 333,
        "old_comparable_observations": 333,
        "additional_case_ids": sorted(additional),
        "comparison": joined,
        "model_configuration_comparison": model,
        "new_business": summarize([r for r in primary["results"] if r["case_id"] in current_ids]),
        "old_business": summarize(old),
        "new_behavior": summarize(
            [
                r
                for r in primary["results"]
                if r["case_id"] not in current_ids and not after[r["case_id"]].get("smoke")
            ]
        ),
        "extension_new": summarize(extension["results"]),
        "extension_old": summarize(extension["legacy_results"]),
        "selection_policy": (
            "Primary new observations remain fixed for all 111 cases; extension new trials are "
            "additional regressions, never substituted to select better results. "
            "Old cases are disjoint and all failures retained."
        ),
        "risk_independence_claim": False,
    }


if __name__ == "__main__":
    primary, extension, output = map(Path, sys.argv[1:])
    result = audit(
        json.loads(primary.read_text(encoding="utf-8")),
        json.loads(extension.read_text(encoding="utf-8")),
    )
    result["sources"] = [
        {"file": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in (primary, extension)
    ]
    with output.open("x", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in {"comparison", "additional_case_ids"}},
            ensure_ascii=False,
        )
    )
