"""Select revised fixtures by version, never by successful outcome; retain original batch."""

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from evaluation.dynamic_metrics import summarize_dynamic
from evaluation.scenarios import load_scenarios
from scripts.audit_v4_release_session import read_jsonl
from scripts.summarize_v4_release_evaluation import summarize, write_markdown


def aggregate(root, supplemental_batch="input-correction-02"):
    original, supplement = root / "formal-01", root / supplemental_batch
    a = json.loads((original / "manifest.json").read_text(encoding="utf-8"))
    b = json.loads((supplement / "manifest.json").read_text(encoding="utf-8"))
    assert a["completed_at"] and b["completed_at"]
    assert a["config"] == b["config"] and a["implementation_sha256"] == b["implementation_sha256"]
    assert a["profiles_hash"] == b["profiles_hash"] and a["repeats"] == b["repeats"] == 2
    changes = json.loads((root / "input-corrections.json").read_text(encoding="utf-8"))
    revised = {c["scenario_id"] for c in changes}
    before, after = (
        load_scenarios(root / "scenarios.json"),
        load_scenarios(root / "scenarios-corrected.json"),
    )
    after_map = {s.scenario_id: s for s in after.scenarios}
    for case in before.scenarios:
        if case.scenario_id not in revised:
            assert case.model_dump() == after_map[case.scenario_id].model_dump()
    originals = read_jsonl(original / "runs.jsonl")
    corrections = read_jsonl(supplement / "runs.jsonl")
    assert len(originals) == 240 and len(corrections) == 4
    assert all(r["scenario_id"] in revised for r in corrections)
    selected = [
        {**r, "source_batch": "formal-01"} for r in originals if r["scenario_id"] not in revised
    ]
    selected += [{**r, "source_batch": supplemental_batch} for r in corrections]
    assert len(selected) == 240 and len({r["run_id"] for r in selected}) == 240
    for run in selected:
        assert run["scenario_version"] == after_map[run["scenario_id"]].scenario_version
    keep = {r["run_id"] for r in selected if r["source_batch"] == "formal-01"}
    calls = [c for c in read_jsonl(original / "calls.jsonl") if c["run_id"] in keep]
    calls += read_jsonl(supplement / "calls.jsonl")
    output = root / "corrected-cohort"
    output.mkdir(exist_ok=False)
    for name, values in (("runs.jsonl", selected), ("calls.jsonl", calls)):
        (output / name).write_text(
            "".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values), encoding="utf-8"
        )
    metrics = summarize_dynamic(selected, calls)
    (output / "summary.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    g = metrics["G4"]
    gate = {
        "passed": (
            g["seller_failure_rate"] <= 0.05
            and g["buyer_failure_rate"] <= 0.05
            and not g["violating_runs"]
            and not any(r["status"] == "BATCH_BUDGET_EXHAUSTED" for r in selected)
        ),
        "seller_failure_rate_limit": 0.05,
        "buyer_failure_rate_limit": 0.05,
    }
    (output / "gate.json").write_text(json.dumps(gate, indent=2), encoding="utf-8")
    original_counts = Counter(r["status"] for r in originals)
    manifest = {
        **a,
        "scenario_set_version": after.version,
        "scenario_set_hash": after.content_hash,
        "scenario_file": "scenarios-corrected.json",
        "derived_from_batches": ["formal-01", supplemental_batch],
        "record_type": "VERSION_SELECTED_DERIVED_COHORT_NOT_NEW_MODEL_CALLS",
        "reserved_cost": str(sum((Decimal(c["reserved_cost"]) for c in calls), Decimal(0))),
        "input_correction": {
            "scenario_ids": sorted(revised),
            "original_seller_failures": original_counts["SELLER_FAILURE"],
            "original_buyer_failures": original_counts["BUYER_SIMULATOR_FAILURE"],
            "original_agreements": original_counts["AGREED"],
        },
        "artifacts_sha256": {
            name: hashlib.sha256((output / name).read_bytes()).hexdigest()
            for name in ("runs.jsonl", "calls.jsonl", "summary.json")
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result = summarize(root, "corrected-cohort")
    write_markdown(root, result)
    print(
        json.dumps(
            {"runs": len(selected), "metrics": result["metrics"], "gate": gate}, ensure_ascii=False
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--supplemental-batch", default="input-correction-02")
    args = parser.parse_args()
    aggregate(args.root, args.supplemental_batch)


if __name__ == "__main__":
    main()
