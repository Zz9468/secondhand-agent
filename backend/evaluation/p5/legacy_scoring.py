"""Normalize legacy result names without changing either version's business contract."""


def score_legacy(case, row):
    if (
        row.get("status") != "OK"
        or row.get("database_evidence") is not True
        or row.get("session_status") != case["expected_status"]
        or any(t.get("status") != "SUCCEEDED" for t in row.get("tasks", []))
    ):
        return False
    aliases = {"NEEDS_SELLER_CONFIRMATION": "WAITING_APPROVAL", "REJECTED": "OFFER_REJECTED"}
    for index, expected in enumerate(case.get("expected_outcomes", [])):
        runs = row.get("runs", [])
        actual = runs[index].get("outcome") if index < len(runs) else None
        if aliases.get(actual, actual) != expected:
            return False
    return True
