"""Recover a stopped batch's journal without discarding uncertain charged calls."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from evaluation.dynamic_metrics import summarize_dynamic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output")
    parser.add_argument("--reason", default="Environment parity correction")
    args = parser.parse_args()
    from pathlib import Path

    output = Path(args.output)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("completed_at"):
        raise ValueError("Refusing to overwrite an already completed batch")
    calls = {}
    for line in (output / "call_events.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            call = json.loads(line)
        except json.JSONDecodeError:
            continue
        calls[call["call_id"]] = call
    for call in calls.values():
        if call["status"] == "STARTED":
            call["status"] = "UNKNOWN"
            call["error_category"] = "PROCESS_INTERRUPTED"
    runs = [
        json.loads(line)
        for line in (output / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    (output / "calls.jsonl").write_text(
        "".join(json.dumps(call, ensure_ascii=False) + "\n" for call in calls.values()),
        encoding="utf-8",
    )
    summary = summarize_dynamic(runs, list(calls.values()))
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    reserve = sum(Decimal(call["reserved_cost"]) for call in calls.values())
    gate = {
        "passed": False,
        "interrupted": True,
        "checks": {
            "runs": len(runs),
            "all_requested_runs_recorded": False,
            "budget_reserved_cost": str(reserve),
            "unknown_usage_calls": sum(call["usage"] is None for call in calls.values()),
        },
    }
    (output / "gate.json").write_text(json.dumps(gate, indent=2), encoding="utf-8")
    manifest.update(
        completed_at=datetime.now(UTC).isoformat(),
        interrupted=True,
        termination_reason=args.reason + "; superseded by a fresh frozen batch",
        reserved_cost=str(reserve),
        artifacts_sha256={
            name: hashlib.sha256((output / name).read_bytes()).hexdigest()
            for name in ("runs.jsonl", "calls.jsonl", "summary.json")
        },
    )
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(gate))


if __name__ == "__main__":
    main()
