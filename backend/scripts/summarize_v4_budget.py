"""Audit all paid V4 batches, retaining interrupted and unknown usage reservations."""

import argparse
import json
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.db.models import ModelExecutionTask
from app.db.session import get_session_factory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=Path("evaluation/results"))
    parser.add_argument("--limit", type=Decimal, default=Decimal("20"))
    args = parser.parse_args()
    rows = []
    for directory in sorted(args.results.glob("v4-real-*")):
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest["model_is_mock"]:
            continue
        assert manifest.get("completed_at"), f"Unfinished paid batch: {directory.name}"
        calls = [
            json.loads(line)
            for line in (directory / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        reserve = sum((Decimal(c["reserved_cost"]) for c in calls), Decimal("0"))
        assert reserve == Decimal(manifest["reserved_cost"]), directory.name
        known = sum(
            (Decimal(c["estimated_cost"]) for c in calls if c["estimated_cost"] is not None),
            Decimal("0"),
        )
        rows.append(
            {
                "batch": directory.name,
                "calls": len(calls),
                "known_estimated_cost": str(known),
                "conservative_allocated_cost": str(reserve),
                "unknown_usage_calls": sum(c["estimated_cost"] is None for c in calls),
                "gate_passed": json.loads((directory / "gate.json").read_text())["passed"],
                "interrupted": manifest.get("interrupted", False),
            }
        )
    for live_dir in sorted(args.results.glob("v4-live-*")):
        setup = json.loads((live_dir / "setup.json").read_text(encoding="utf-8"))
        assert (live_dir / "report.json").exists(), f"Unfinished live check: {live_dir.name}"
        live_report = json.loads((live_dir / "report.json").read_text(encoding="utf-8"))
        # A live session can be reused. Freeze accounting to this probe's tasks.
        task_ids = setup.get("task_ids") or [
            case["task_id"] for case in live_report.get("cases", []) if case.get("task_id")
        ]
        with get_session_factory()() as db:
            query = select(ModelExecutionTask).where(
                ModelExecutionTask.session_id == setup["session_id"]
            )
            if task_ids:
                query = query.where(ModelExecutionTask.id.in_(task_ids))
            tasks = list(db.scalars(query))
            assert tasks and all(t.estimated_cost is not None for t in tasks)
            assert not task_ids or {t.id for t in tasks} == set(task_ids)
            known = sum((t.estimated_cost for t in tasks), Decimal("0"))
            # V4 reserves before calls; legacy followups have actual known usage.
            reserve = sum(
                (
                    Decimal(t.runtime_checkpoint["reserved_cost"])
                    if t.runtime_checkpoint
                    else t.estimated_cost
                    for t in tasks
                ),
                Decimal("0"),
            )
            assert reserve >= known
        rows.append(
            {
                "batch": live_dir.name,
                "known_estimated_cost": str(known),
                "conservative_allocated_cost": str(reserve),
                "unknown_usage_calls": 0,
                "gate_passed": live_report.get("status") != "FAIL",
                "interrupted": False,
            }
        )
    known_total = sum(Decimal(r["known_estimated_cost"]) for r in rows)
    reserve_total = sum(Decimal(r["conservative_allocated_cost"]) for r in rows)
    report = {
        "currency": "CNY",
        "limit": str(args.limit),
        "known_estimated_cost": str(known_total),
        "conservative_allocated_cost": str(reserve_total),
        "unknown_usage_calls": sum(r["unknown_usage_calls"] for r in rows),
        "within_limit": reserve_total <= args.limit,
        "batches": rows,
        "billing_note": "Frozen-price estimates; unknown calls retain full reservation. "
        "Final invoice belongs to the provider. Mock batches and prior V3 work are excluded.",
    }
    (args.results / "v4-budget-ledger.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    assert report["within_limit"], "Authorized cumulative budget exceeded"


if __name__ == "__main__":
    main()
