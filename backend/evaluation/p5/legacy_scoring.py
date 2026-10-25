"""Normalize legacy result names without changing either version's business contract."""

from decimal import Decimal


def score_legacy(case, row):
    if (
        row.get("status") != "OK"
        or row.get("database_evidence") is not True
        or row.get("session_status") != case["expected_status"]
        or any(t.get("status") != "SUCCEEDED" for t in row.get("tasks", []))
    ):
        return False
    aliases = {
        "NEEDS_SELLER_CONFIRMATION": "WAITING_APPROVAL",
        "REJECTED": "OFFER_REJECTED",
        "RULE_BLOCKED": "NO_ACTION",
        "SAFE_INFORMATION": "NO_ACTION",
    }
    for index, expected in enumerate(case.get("expected_outcomes", [])):
        runs = row.get("runs", [])
        actual = runs[index].get("outcome") if index < len(runs) else None
        if aliases.get(actual, actual) != expected:
            return False
    if case.get("source_scenario"):
        for spec, expected in zip(case["scripted_offers"], case["expected_outcomes"], strict=True):
            if not spec or expected == "NO_ACTION":
                continue
            matching = [
                o
                for o in row.get("offers", [])
                if (
                    Decimal(o["amount"]) == Decimal(spec["price"])
                    and o["shipping_paid_by"] == spec["shipping_paid_by"]
                    and Decimal(o["seller_discount"]) == Decimal(spec["seller_borne_discount"])
                    and o["terms"] == {k: str(v) for k, v in spec["additional_terms"].items()}
                    and (
                        spec["shipping_paid_by"] != "seller"
                        or o["shipping_cost"] is not None
                        and Decimal(o["shipping_cost"]) == Decimal(spec["shipping_cost"])
                    )
                )
            ]
            if not matching:
                return False
        for offer in row.get("offers", []):
            if offer["status"] == "ACCEPTED":
                shipping = offer["shipping_cost"] if offer["shipping_paid_by"] == "seller" else "0"
                if shipping is None or Decimal(offer["amount"]) - Decimal(shipping) - Decimal(
                    offer["seller_discount"]
                ) < Decimal(case["policy"]["minimum_net_price"]):
                    return False
        if "WAITING_APPROVAL" in case["expected_outcomes"]:
            expected = {"approve": "APPROVED", "reject": "REJECTED", "expire": "EXPIRED"}[
                case["seller_review"]
            ]
            if not row.get("approvals") or any(a["status"] != expected for a in row["approvals"]):
                return False
        if row["session_status"] == "AGREED":
            offer = next(
                (o for o in row.get("offers", []) if o["id"] == row.get("confirmed_offer_id")), None
            )
            goal = case["buyer_goal"]
            if (
                not goal["auto_confirm_valid_offer"]
                or not offer
                or Decimal(offer["amount"]) > Decimal(goal["maximum_price"])
            ):
                return False
            if any(offer["terms"].get(k) != v for k, v in goal["required_terms"].items()):
                return False
    return True
