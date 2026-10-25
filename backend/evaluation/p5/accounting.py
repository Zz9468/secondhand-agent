"""Append explicit, non-overlapping experiment ledgers to a saved cumulative baseline."""

import argparse
import json
from decimal import Decimal
from pathlib import Path

from evaluation.p5.runner import sha, summarize


def collect(baseline, reports, standalone):
    total = dict(json.loads(baseline.read_text(encoding="utf-8"))["stage_usage"])
    sources, seen_hashes, seen_runs = [], set(), set()
    mapping = {
        "seller_calls": "recorded_seller_calls",
        "auxiliary_calls": "recorded_auxiliary_calls",
        "tokens": "known_tokens",
        "unknown_usage_calls": "unknown_usage_calls",
        "unknown_cost_calls": "unknown_cost_calls",
        "unrecovered_seller_accounting_rows": "unrecovered_seller_accounting_rows",
    }
    for path, is_report in [(p, True) for p in reports] + [(p, False) for p in standalone]:
        fingerprint = sha(path)
        if fingerprint in seen_hashes:
            raise ValueError("duplicate ledger file")
        seen_hashes.add(fingerprint)
        data = json.loads(path.read_text(encoding="utf-8"))
        if is_report:
            rows = data["results"] + data.get("legacy_results", [])
            run_ids = {r["run_id"] for row in rows for r in row.get("runs", []) if "run_id" in r}
            if seen_runs & run_ids:
                raise ValueError("reassessment overlaps a previously counted seller run")
            seen_runs |= run_ids
        else:
            # Standalone annotation calls are auxiliary; there are no implied seller calls.
            rows = [{"status": "OK", "database_evidence": True, "auxiliary_calls": data}]
        summary = summarize(rows)
        for source, dest in mapping.items():
            total[dest] = total.get(dest, 0) + summary[source]
        total["known_estimated_cny"] = str(
            Decimal(total["known_estimated_cny"]) + Decimal(summary["known_estimated_cny"])
        )
        sources.append({"file": str(path), "sha256": fingerprint, "usage": summary})
    total["notes"] = (
        "Saved baseline plus explicit non-overlapping new batches and standalone annotation calls. "
        "Original baseline unknown RPC count retained. Failed calls included; unknown costs are "
        "not zero. Estimates are not provider invoices. Raw reports/ledgers remain authoritative."
    )
    return {
        "baseline": {"file": str(baseline), "sha256": sha(baseline)},
        "sources": sources,
        "stage_usage": total,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--reports", nargs="+", type=Path, required=True)
    parser.add_argument("--standalone", nargs="*", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = collect(args.baseline, args.reports, args.standalone)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result["stage_usage"], ensure_ascii=False))


if __name__ == "__main__":
    main()
