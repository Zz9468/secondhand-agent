"""External model settings for an isolated, unmodified legacy source checkout."""

from copy import deepcopy

SHARED_MODEL_CONFIG = {
    "max_tokens": 2048,
    "temperature": 0,
    "enable_thinking": False,
    "sdk_retries": 0,
    "timeout_cap_seconds": 45,
}


def aligned_config(config):
    value = deepcopy(config)
    value["model"].update(temperature=0, timeout=45, thinking=False)
    value["budget"]["output_tokens"] = 2048
    if value.get("reply_review"):
        value["reply_review"].update(output_tokens=2048, thinking=False)
    return value


def public_call_config(serialized, invocation):
    # Never retain the complete serialized model: it may contain secrets or endpoints.
    frozen = (serialized or {}).get("kwargs", {})
    extra = invocation.get("extra_body") or frozen.get("extra_body") or {}
    return {
        "model": invocation.get("model") or frozen.get("model_name"),
        "max_tokens": invocation.get("max_completion_tokens", invocation.get("max_tokens")),
        "temperature": invocation.get("temperature"),
        "enable_thinking": extra.get("enable_thinking"),
        "sdk_retries": frozen.get("max_retries"),
        "timeout_seconds": frozen.get("request_timeout"),
    }


def configuration_audit(rows, expected_model):
    calls = [c for row in rows for c in row.get("recorded_calls", [])]
    missing, mismatch = [], []
    for index, call in enumerate(calls):
        config = call.get("model_config") or {}
        if any(
            config.get(k) is None
            for k in (
                "model",
                "max_tokens",
                "temperature",
                "enable_thinking",
                "sdk_retries",
                "timeout_seconds",
            )
        ):
            missing.append(index)
            continue
        if (
            any(
                config[k] != v for k, v in SHARED_MODEL_CONFIG.items() if k != "timeout_cap_seconds"
            )
            or config["model"] != expected_model
            or not 0 < config["timeout_seconds"] <= SHARED_MODEL_CONFIG["timeout_cap_seconds"]
        ):
            mismatch.append(index)
    return {
        "status": "FAIL"
        if mismatch
        else "INSUFFICIENT_EVIDENCE"
        if missing or not calls
        else "PASS",
        "scope": (
            "Actual legacy provider callbacks; new provider parameters verified "
            "in frozen runtime source"
        ),
        "shared": SHARED_MODEL_CONFIG,
        "observed_legacy_calls": len(calls),
        "missing_call_indices": missing,
        "mismatching_call_indices": mismatch,
        "timeout_note": (
            "New calls also respect the remaining per-input activity/reply deadline (cap 45s)."
        ),
    }
