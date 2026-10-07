from collections import Counter
from decimal import Decimal


def summarize_dynamic(runs, calls):
    groups = {}
    for group in ("G0", "G1", "G2", "G3", "G4"):
        selected = [r for r in runs if r["group"] == group]
        if not selected:
            continue
        feasible = [r for r in selected if r["feasible"]]
        agreed = [r for r in selected if r["status"] == "AGREED"]
        net = [
            Decimal(r["seller_net_income"]) for r in agreed if r["seller_net_income"] is not None
        ]
        relevant_calls = [c for c in calls if c["run_id"] in {r["run_id"] for r in selected}]
        unknown_usage = [c for c in relevant_calls if c["usage"] is None]
        groups[group] = {
            "runs": len(selected),
            "statuses": dict(Counter(r["status"] for r in selected)),
            "agreement_rate_all_runs": len(agreed) / len(selected),
            "violating_runs": sum(bool(r["violations"]) for r in selected),
            "safety_observation_count": sum(
                len(r.get("safety_observations", [])) for r in selected
            ),
            "safety_observed_runs": sum(bool(r.get("safety_observations")) for r in selected),
            "feasible_runs": len(feasible),
            "agreement_rate_on_feasible": (
                sum(r["status"] == "AGREED" for r in feasible) / len(feasible)
            )
            if feasible
            else None,
            "effective_end_rate": sum(r["status"] in {"AGREED", "CLOSED_VALID"} for r in selected)
            / len(selected),
            "seller_failure_rate": sum(r["status"] == "SELLER_FAILURE" for r in selected)
            / len(selected),
            "buyer_failure_rate": sum(r["status"] == "BUYER_SIMULATOR_FAILURE" for r in selected)
            / len(selected),
            "safety_violations": sum(len(r["violations"]) for r in selected),
            "mean_net_income_on_agreed": str(sum(net) / len(net)) if net else None,
            "repeated_buyer_questions": sum(r["repeated_questions"] for r in selected),
            "tool_input_rejections": sum(r["tool_input_rejections"] for r in selected),
            "memory_update_failures": sum(r["memory_update_failures"] for r in selected),
            "memory_update_failure_events": sum(r["memory_update_failure_events"] for r in selected)
            if all("memory_update_failure_events" in r for r in selected)
            else None,
            "model_calls_by_purpose": dict(Counter(c["purpose"] for c in relevant_calls)),
            "known_tokens": sum(
                c["usage"].get("total_tokens", 0) for c in relevant_calls if c["usage"]
            ),
            "unknown_usage_calls": len(unknown_usage),
            "known_estimated_cost": str(
                sum(
                    Decimal(c["estimated_cost"])
                    for c in relevant_calls
                    if c["estimated_cost"] is not None
                )
            ),
            "mean_seller_latency_ms": sum(r["seller_latency_ms"] for r in selected) / len(selected),
        }
    return groups
