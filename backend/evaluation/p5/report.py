"""Local release gate: a completed experiment is never itself a release approval."""

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from evaluation.p5.runner import sha, summarize
from evaluation.p5.scoring import acceptance, risk_report


def junit_record(path):
    root = ET.parse(path).getroot()
    tests = list(root.iter("testcase"))
    failures = sum(t.find("failure") is not None or t.find("error") is not None for t in tests)
    skipped = sum(t.find("skipped") is not None for t in tests)
    return {
        "file": path.name,
        "sha256": sha(path),
        "tests": len(tests),
        "failures": failures,
        "skipped": skipped,
        "status": "FAIL"
        if failures
        else "PASS"
        if tests and not skipped
        else "INSUFFICIENT_EVIDENCE",
    }


def build(report, cases, tests):
    rows = report["results"]
    risks = risk_report(
        cases,
        rows,
        remote_required=True,
        development_families=[c["family"] for c in cases if c["split"] == "development"],
    )
    smoke_ids = {c["case_id"] for c in cases if c.get("smoke")}
    smoke = [r for r in rows if r["case_id"] in smoke_ids]
    smoke_status = (
        "FAIL"
        if any(not r.get("business_pass") for r in smoke)
        else (
            "PASS"
            if {r["case_id"] for r in smoke} == smoke_ids and len(smoke_ids) >= 200
            else "INSUFFICIENT_EVIDENCE"
        )
    )
    quality = report["quality"]
    quality_status = (
        "INSUFFICIENT_EVIDENCE"
        if any(v is None for v in quality.values())
        else "PASS"
        if all(v >= 0.95 for v in quality.values())
        else "FAIL"
    )
    remote_status = (
        "PASS"
        if len(report["remote"]) >= 3
        and all(x["status"] == "PASS" for x in report["remote"])
        and report.get("native_trace", {}).get("status") == "PASS"
        else "INSUFFICIENT_EVIDENCE"
    )
    statuses = [t["status"] for t in tests]
    deterministic = (
        "FAIL"
        if "FAIL" in statuses
        else "PASS"
        if len(tests) >= 2 and all(s == "PASS" for s in statuses)
        else "INSUFFICIENT_EVIDENCE"
    )
    cases_by_id = {c["case_id"]: c for c in cases}
    missing_repetitions = [
        c["case_id"]
        for c in cases
        if not c.get("smoke")
        and len({r["repeat"] for r in rows if r["case_id"] == c["case_id"]}) < 3
    ]
    business_failures = [
        {
            "case_id": r["case_id"],
            "repeat": r["repeat"],
            "status": r["status"],
            "failures": r.get("failures", []),
        }
        for r in rows
        if not r.get("business_pass")
    ]
    judged = [r for r in rows if not cases_by_id[r["case_id"]].get("smoke")]
    judge_incomplete = any(type(r.get("judge", {}).get("score")) is not bool for r in judged)
    components = {
        "deterministic": deterministic,
        "development_200": smoke_status,
        "quality": quality_status,
        "remote": remote_status,
        "comparison": report["comparison"]["status"],
        "business_cases": "FAIL" if business_failures else "PASS",
        "three_repetitions": "INSUFFICIENT_EVIDENCE" if missing_repetitions else "PASS",
        "judge_completeness": "INSUFFICIENT_EVIDENCE" if judge_incomplete else "PASS",
        "frozen_runtime": report.get("frozen_runtime", {}).get("status", "INSUFFICIENT_EVIDENCE"),
        "human_quality_and_commitment_review": "INSUFFICIENT_EVIDENCE",
    }
    overall = acceptance(
        risks,
        deterministic=deterministic,
        quality=quality_status,
        remote=remote_status,
        comparison=report["comparison"]["status"],
    )
    if "FAIL" in components.values():
        overall = "FAIL"
    elif any(s != "PASS" for s in components.values()) and overall == "PASS":
        overall = "INSUFFICIENT_EVIDENCE"
    return {
        "release_status": overall,
        "components": components,
        "risk": risks,
        "metadata": report["metadata"],
        "summary": summarize(rows),
        "stratified_metrics": {
            "new_business": summarize(
                [r for r in rows if cases_by_id[r["case_id"]]["group"] == "business_preserving"]
            ),
            "old_business": summarize(report.get("legacy_results", [])),
            "new_behavior": summarize(
                [
                    r
                    for r in rows
                    if cases_by_id[r["case_id"]]["group"] != "business_preserving"
                    and not cases_by_id[r["case_id"]].get("smoke")
                ]
            ),
            "development_smoke": summarize(smoke),
        },
        "per_case": {
            cid: summarize([r for r in rows if r["case_id"] == cid])
            for cid in cases_by_id
            if not cases_by_id[cid].get("smoke")
        },
        "judge_failures": [
            {"case_id": r["case_id"], "repeat": r["repeat"], "comment": r["judge"].get("comment")}
            for r in rows
            if r.get("judge", {}).get("score") is False
        ],
        "quality": quality,
        "tests": tests,
        "missing_repetitions": missing_repetitions,
        "business_failures": business_failures,
        "remote": report["remote"],
        "native_trace": report.get("native_trace"),
        "comparison": report["comparison"],
        "scope": "Synthetic candidates; no certified representative risk corpus",
    }


def main():
    from evaluation.p5.datasets import load_cases

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--junit", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = build(
        json.loads(args.report.read_text(encoding="utf-8")),
        json.loads(args.report.read_text(encoding="utf-8")).get("cases") or load_cases(),
        [junit_record(p) for p in args.junit],
    )
    value["report_sha256"] = sha(args.report)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    print(value["release_status"])
    return 0 if value["release_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
