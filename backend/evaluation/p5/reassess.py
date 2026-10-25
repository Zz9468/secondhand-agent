"""Reassess every saved core conversation; preserve the original batch and all judge attempts."""

import argparse
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from app.runtime.contracts import digest
from evaluation.p5.datasets import load_cases
from evaluation.p5.legacy_scoring import score_legacy
from evaluation.p5.official import export_experiment, judge
from evaluation.p5.references import public_fixture_reference
from evaluation.p5.runner import comparison, sha, summarize
from evaluation.p5.scoring import risk_report


def reassess(source, destination, *, concurrency=4):
    destination.mkdir(parents=True, exist_ok=False)
    report = json.loads(source.read_text(encoding="utf-8"))
    cases = load_cases()
    if report["metadata"]["dataset_sha256"] != digest(cases):
        raise ValueError("dataset changed: cannot reassess against different cases")
    by_id = {c["case_id"]: c for c in cases}
    report["reassessment"] = {
        "source_report": str(source.resolve()),
        "source_sha256": sha(source),
        "seller_execution_commit": report["metadata"]["source_commit"],
        "started_at": datetime.now(UTC).isoformat(),
        "changes": [
            "legacy REJECTED enum alias",
            "complete public fixture reference for all judges",
        ],
        "policy": (
            "All core/legacy rows reassessed; no seller replay or case selection; "
            "prior scores retained"
        ),
    }
    report["metadata"]["evaluator_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    report["metadata"]["judge_reference_sha256"] = digest(
        {c["case_id"]: public_fixture_reference(c) for c in cases}
    )
    report["original_remote"] = report.pop("remote")
    report["remote"] = []
    report.pop("release_status", None)
    lock = threading.Lock()

    def save():
        temp = destination / "report.json.tmp"
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(destination / "report.json")

    save()

    def evaluate(original):
        row = deepcopy(original)
        case = by_id[row["case_id"]]
        row.setdefault("previous_judges", []).append(row.pop("judge", {}))
        if row["target"] == "legacy":
            row["original_business_pass"] = row["business_pass"]
            row["business_pass"] = score_legacy(case, row)
        try:
            row["judge"] = judge(
                case,
                row["messages"] if row["target"] == "legacy" else row["trajectory"],
                row["auxiliary_calls"],
            )
        except Exception as exc:
            row["judge"] = {"score": None, "error_type": type(exc).__name__}
        with lock:
            original.clear()
            original.update(row)
            save()

    judged = [r for r in report["results"] if not by_id[r["case_id"]].get("smoke")]
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(evaluate, judged + report["legacy_results"]))
    comparable = [c for c in cases if c["group"] == "business_preserving"]
    ids = {c["case_id"] for c in comparable}
    for label, rows, dataset in (
        ("graph", report["results"], "full"),
        ("graph-business", [r for r in report["results"] if r["case_id"] in ids], "business"),
        ("legacy-business", report["legacy_results"], "business"),
    ):
        try:
            remote = export_experiment(rows, report["metadata"], label=label, dataset_label=dataset)
        except Exception as exc:
            remote = {"status": "INSUFFICIENT_EVIDENCE", "error_type": type(exc).__name__}
        remote["target"] = label
        report["remote"].append(remote)
        for row in rows:
            row["remote_returned"] = remote["status"] == "PASS"
        save()
    report["comparison"] = comparison(comparable, report["results"], report["legacy_results"])
    report["summary"] = summarize(report["results"])
    report["risk"] = risk_report(
        cases,
        report["results"],
        remote_required=True,
        development_families=[c["family"] for c in cases if c["split"] == "development"],
    )
    report["reassessment"]["finished_at"] = datetime.now(UTC).isoformat()
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-real-model", action="store_true", required=True)
    args = parser.parse_args()
    result = reassess(args.source, args.output)
    print(json.dumps({"remote": [r["status"] for r in result["remote"]]}))
    return 0 if all(r["status"] == "PASS" for r in result["remote"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
