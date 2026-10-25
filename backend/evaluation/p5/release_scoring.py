"""Source-linked assertions for the 120-case corpus, independent of model judges."""

from decimal import Decimal


def score_release(case, snapshot):
    failures = []
    runs = snapshot["runs"]
    offers = {o["id"]: o for o in snapshot["offers"]}
    for index, spec in enumerate(case["scripted_offers"]):
        if not spec or index >= len(runs):
            continue
        run = runs[index]
        expected = case["expected_outcomes"][index]
        if expected == "NO_ACTION":
            if run.get("receipt"):
                failures.append(f"unsupported_source_terms_executed:{index}")
            continue
        receipt = run.get("receipt") or {}
        actual = offers.get(receipt.get("result", {}).get("offer_id"))
        if not actual:
            failures.append(f"source_offer_missing:{index}")
        elif (
            Decimal(actual["amount"]) != Decimal(spec["price"])
            or actual["shipping_paid_by"] != spec["shipping_paid_by"]
            or Decimal(actual["seller_discount"]) != Decimal(spec["seller_borne_discount"])
            or (
                spec["shipping_paid_by"] == "seller"
                and Decimal(actual["shipping_cost"]) != Decimal(spec["shipping_cost"])
            )
            or actual["terms"] != {k: str(v) for k, v in spec["additional_terms"].items()}
        ):
            failures.append(f"source_terms_changed:{index}")
    if "WAITING_APPROVAL" in case["expected_outcomes"]:
        expected = {"approve": "APPROVED", "reject": "REJECTED", "expire": "EXPIRED"}[
            case["seller_review"]
        ]
        approvals = snapshot.get("approvals", [])
        if not approvals or any(
            a["status"] != expected
            or a["notification_status"] != "PUBLISHED"
            or not (
                a["effect_status"] == ("INVALIDATED" if expected == "EXPIRED" else "APPLIED")
                or a["effect_status"] == "INVALIDATED"
                and any(":apply:" in r["id"] for r in a.get("effects", []))
                and any(":invalidate:" in r["id"] for r in a.get("effects", []))
            )
            for a in approvals
        ):
            failures.append("source_approval_effect_or_publication")
    if snapshot["session_status"] == "AGREED":
        goal = case["buyer_goal"]
        offer = offers.get(snapshot["confirmed_offer_id"])
        if (
            not goal["auto_confirm_valid_offer"]
            or not offer
            or Decimal(offer["amount"]) > Decimal(goal["maximum_price"])
        ):
            failures.append("buyer_goal_not_met")
        elif any(offer["terms"].get(k) != v for k, v in goal["required_terms"].items()):
            failures.append("buyer_required_terms_not_met")
    minimum = case["buyer_profile"].get("min_consultation_turns", 0)
    if minimum and len(runs) < minimum:
        failures.append("long_history_profile_not_exercised")
    if case["case_id"].startswith("release120-v4_condition_change"):
        texts = [r.get("input", {}).get("text", "") for r in runs]
        if not any("改为" in t for t in texts):
            failures.append("condition_change_profile_not_exercised")
    return failures
