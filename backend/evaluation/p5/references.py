"""Public reference facts of the actual P5 fixture, never private authorization thresholds."""


def public_fixture_reference(case):
    if case.get("product"):
        return {
            **case["product"],
            "seller_description": case["product"]["description"],
            "verified_facts": case.get("public_facts", {}),
        }
    if case["fixture_ref"] != "phone-policy-750-850-v1":
        raise ValueError("unknown public fixture reference")
    return {
        "title": "P5二手手机",
        "listed_price": "1000",
        "seller_description": "电池健康92%；维修情况未知；支持自提与快递。",
        "verified_facts": {
            "allowed_delivery_methods": ["pickup", "shipping"],
            **case.get("public_facts", {}),
        },
    }
