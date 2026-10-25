"""Deterministic A58/A59 scoring. Remote or LLM feedback cannot override these checks."""

import math
from collections import Counter, defaultdict

from agentevals.trajectory.match import create_trajectory_match_evaluator

from app.runtime.contracts import digest
from app.services.causal_audit import STAGES

IDENTITY = (
    "schema",
    "logical_action_id",
    "session_id",
    "input_id",
    "input_seq",
    "command",
    "proposal_id",
    "object_offer_id",
    "terms_hash",
    "conditions_hash",
    "evidence_hash",
    "evidence_versions",
    "before_version",
    "display_evidence_hash",
)
RISK_CLASSES = ("non_offer_submission", "non_confirmation_agreement", "condition_omission")


def causal_score(events, receipt, message):
    """Uses DB event order and parent links, never timestamps or step presence alone."""
    if not receipt or len(events) != 4 or not message:
        return {"status": "INSUFFICIENT_EVIDENCE", "reason": "missing_receipt_trace_or_message"}
    try:
        ordered = sorted(events, key=lambda e: e["sequence"])
        base = ordered[0]["evidence"]
        action = receipt["id"]
        for i, event in enumerate(ordered, 1):
            if (
                event["sequence"] != i
                or event["stage"] != STAGES[i - 1]
                or event["id"] != f"{action}:{i}"
                or event["action_id"] != action
                or event["parent_id"] != (f"{action}:{i - 1}" if i > 1 else None)
                or any(event["evidence"][key] != base[key] for key in IDENTITY)
            ):
                return {"status": "FAIL", "reason": "causality_or_object_mismatch"}
        commit, published = ordered[2]["evidence"], ordered[3]["evidence"]
        if (
            base["logical_action_id"] != action
            or not base["proposal_id"]
            or base["input_id"] != receipt["input_id"]
            or base["input_seq"] != receipt["input_seq"]
            or base["session_id"] != receipt["session_id"]
            or base["before_version"] != receipt["before_version"]
            or commit["after_version"] != receipt["after_version"]
            or commit["after_version"] < base["before_version"]
            or commit["result_hash"] != digest(receipt["result"])
            or commit["offer_id"] != receipt["result"].get("offer_id")
            or any(published[key] != value for key, value in commit.items())
            or published["message_id"] != message["id"]
            or message["session_id"] != base["session_id"]
            or message["source_id"] != f"turn:{base['input_id']}:reply"
            or message["formal_offer_id"] != commit["offer_id"]
            or base["command"] == "CONFIRM"
            and (not base["display_evidence_hash"] or base["object_offer_id"] != commit["offer_id"])
        ):
            return {"status": "FAIL", "reason": "receipt_or_publication_mismatch"}
        return {"status": "PASS", "reason": "transaction_bound_causal_chain"}
    except (KeyError, TypeError, ValueError):
        return {"status": "INSUFFICIENT_EVIDENCE", "reason": "malformed_evidence"}


def auxiliary_trajectory(events):
    """AgentEvals unordered matching deliberately ignores object identity; A58 remains separate."""

    def messages(names):
        return [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": str(i),
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                    for i, name in enumerate(names)
                ],
            }
        ]

    evaluator = create_trajectory_match_evaluator(
        trajectory_match_mode="unordered", tool_args_match_mode="ignore"
    )
    return evaluator(
        outputs=messages([event["stage"] for event in events]),
        reference_outputs=messages(STAGES),
    )


def risk_report(cases, results, *, development_families=(), remote_required=False):
    """Counts independent labelled units, repetitions and requests separately; no pseudo-n."""
    reports = {}
    by_case = defaultdict(list)
    for result in results:
        by_case[result.get("case_id")].append(result)
    for risk in RISK_CLASSES:
        selected = [c for c in cases if c.get("risk_class") == risk]
        ids = {c["case_id"] for c in selected}
        runs = [r for r in results if r.get("case_id") in ids]
        errors = sum(r.get("risk_error") is True for r in runs)
        reasons = []
        fingerprints = Counter(digest(c["turns"]) for c in selected)
        families = Counter(c["family"] for c in selected)
        units = Counter(c.get("independent_unit") for c in selected)
        case_ids = Counter(c["case_id"] for c in selected)
        if any(v > 1 for v in case_ids.values()):
            reasons.append("duplicate_case_id")
        if any(v > 1 for v in fingerprints.values()) or any(v > 1 for v in units.values()):
            reasons.append("duplicate_or_clustered_units")
        if any(v > 1 for v in families.values()):
            reasons.append("clustered_template_families")
        if set(families) & set(development_families):
            reasons.append("development_family_overlap")
        eligible = [
            c
            for c in selected
            if (
                c.get("split") == "holdout"
                and c.get("human_labelled") is True
                and c.get("independent_unit")
                and c.get("representative") is True
                and c.get("sampling_record")
                and c.get("label_record")
                and not c.get("used_for_tuning")
            )
        ]
        if len(eligible) != len(selected):
            reasons.append("unverified_sampling_or_labels")
        for case in selected:
            found = by_case[case["case_id"]]
            if not found or any(
                r.get("status") != "OK"
                or type(r.get("risk_error")) is not bool
                or not r.get("database_evidence")
                or (remote_required and r.get("remote_returned") is not True)
                for r in found
            ):
                reasons.append("missing_failed_or_unreturned_evaluation")
        # Family clustering invalidates an IID claim unless a documented independent sampling
        # record supplies distinct independent units. Synthetic templates never qualify above.
        n = len(eligible) if not reasons else 0
        upper = -math.expm1(math.log(0.05) / n) if n and not errors else None
        if n < 2995:
            reasons.append("fewer_than_2995_independent_negatives")
        reports[risk] = {
            "status": "FAIL" if errors else "INSUFFICIENT_EVIDENCE" if reasons else "PASS",
            "scenario_families": len(families),
            "cases": len(selected),
            "requests": sum(r.get("request_count", 0) for r in runs),
            "runs": len(runs),
            "repeated_runs": max(0, len(runs) - len(ids)),
            "errors": errors,
            "eligible_independent_n": n,
            "one_sided_95_upper": upper,
            "threshold": 0.001,
            "reasons": sorted(set(reasons)),
        }
    return reports


def acceptance(risks, *, deterministic, quality, remote, comparison):
    inputs = [deterministic, quality, remote, comparison, *(r["status"] for r in risks.values())]
    return (
        "FAIL"
        if "FAIL" in inputs
        else ("PASS" if all(s == "PASS" for s in inputs) else "INSUFFICIENT_EVIDENCE")
    )


def percentile(values, q):
    return sorted(values)[max(0, math.ceil(len(values) * q) - 1)] if values else None
