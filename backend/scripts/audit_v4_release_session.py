"""Audit this newly authorized session, including incomplete batches and unknown calls."""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path


def read_jsonl(path):
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    # A live process can have a partially flushed last line; completed lines remain authoritative.
    records = []
    for index, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if index != len(lines) - 1:
                raise
    return records


def audit(root):
    batches = []
    for manifest_path in sorted(root.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("model_is_mock", True) or manifest.get("derived_from_batches"):
            continue
        directory = manifest_path.parent
        latest = {}
        for event in read_jsonl(directory / "call_events.jsonl"):
            latest[event["call_id"]] = event
        persisted = read_jsonl(directory / "calls.jsonl")
        for call in persisted:
            latest[call["call_id"]] = call
        calls = list(latest.values())
        reserve = sum((Decimal(c["reserved_cost"]) for c in calls), Decimal("0"))
        known = sum(
            (Decimal(c["estimated_cost"]) for c in calls if c.get("estimated_cost") is not None),
            Decimal("0"),
        )
        assert reserve >= known
        completed = bool(manifest.get("completed_at"))
        if completed:
            assert len(persisted) == len(calls), directory.name
            assert Decimal(manifest["reserved_cost"]) == reserve, directory.name
            for name, expected in manifest["artifacts_sha256"].items():
                assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == expected
        batches.append(
            {
                "batch": directory.name,
                "completed": completed,
                "calls": len(calls),
                "known_cost_cny": str(known),
                "reserved_cost_cny": str(reserve),
                "unknown_calls": sum(c.get("estimated_cost") is None for c in calls),
            }
        )
    known = sum((Decimal(b["known_cost_cny"]) for b in batches), Decimal("0"))
    reserved = sum((Decimal(b["reserved_cost_cny"]) for b in batches), Decimal("0"))
    ledger = {
        "new_authorized_limit_cny": "20",
        "scope": str(root),
        "includes_previous_project_allowance": False,
        "known_cost_cny": str(known),
        "reserved_cost_cny": str(reserved),
        "remaining_cny": str(Decimal("20") - reserved),
        "within_budget": reserved <= Decimal("20"),
        "batches": batches,
    }
    (root / "budget-ledger.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ledger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    result = audit(args.root)
    print(json.dumps(result, ensure_ascii=False))
    assert result["within_budget"]


if __name__ == "__main__":
    main()
