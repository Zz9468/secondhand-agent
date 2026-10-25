"""Strict, pinned-SDK export policy for the P0 native LangSmith integration.

No free-form text, private numeric fields, events, serialized objects or attachments
are exported. Richer redaction can be introduced in P4 with boundary regressions.
"""

import os
import re
from uuid import UUID

from langsmith import Client
from urllib3.util.retry import Retry

SAFE_NAMES = {
    "wait_for_seller", "apply_result", "publish_notice",
    "prepare_context",
    "parse_intents",
    "plan_intents",
    "commit_business",
    "publish_reply",
    "compose_reply",
    "review_reply",
    "load_approval",
    "await_decision",
    "apply_decision",
    "publish_approval",
    "p4_error",
    "p4_attachment",
    "LangGraph",
    "p0_trace",
    "query_product",
    "semantic",
    "commit",
    "publish",
    "ChatOpenAI",
    "OpenAI",
    "ChatCompletion",
    "p0_error",
    "p0_attachment",
}
SAFE_VALUES = {
    "negotiation-v1",
    "negotiation-v2",
    "negotiation-v3",
    "approval-v1",
    "p4-v1",
    "ACCEPTED",
    "UNKNOWN",
    "STALE",
    "REVIEW_REJECTED",
    "BUDGET_EXHAUSTED",
    "ASK_PRODUCT",
    "PROPOSE_OFFER",
    "NONE",
    "pickup",
    "shipping",
    "qwen3.7-flash",
    "p0-graph-v1",
    "p0-config-v1",
    "passed",
    "chain",
    "llm",
    "tool",
}
COUNTERS = {
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "review_count",
}
CONTENT_KEYS = COUNTERS | {"intent", "model", "graph_version", "status", "delivery", "run_ref"}
ENVELOPE = {
    "id",
    "trace_id",
    "parent_run_id",
    "dotted_order",
    "start_time",
    "end_time",
    "session_name",
    "session_id",
    "project_name",
    "reference_example_id",
}


def safe_content(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in CONTENT_KEYS & value.keys():
        item = value[key]
        if key == "run_ref" and isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item):
            result[key] = item
            continue
        if (
            key in COUNTERS
            and isinstance(item, int)
            and 0 <= item <= 10_000_000
            or isinstance(item, str)
            and item in SAFE_VALUES
        ):
            result[key] = item
    return result


def safe_envelope(key, value):
    if key in {"id", "trace_id", "parent_run_id", "session_id", "reference_example_id"}:
        try:
            UUID(str(value))
            return True
        except (ValueError, TypeError):
            return False
    if key in {"project_name", "session_name"}:
        return value in {"secondhand-agent-langgraph-dev", "secondhand-agent-langgraph-test"}
    if key == "dotted_order":
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-fTZ.:-]+", value))
    if key in {"start_time", "end_time"}:
        from datetime import datetime

        return isinstance(value, datetime) or (
            isinstance(value, str) and bool(re.fullmatch(r"[0-9TZ.+:-]+", value))
        )
    return False


def safe_run(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    result = {
        key: item for key, item in value.items() if key in ENVELOPE and safe_envelope(key, item)
    }
    for key in ("inputs", "outputs"):
        if key in value:
            result[key] = safe_content(value[key])
    if "name" in value:
        result["name"] = value["name"] if value["name"] in SAFE_NAMES else "p0_trace"
    if "run_type" in value:
        result["run_type"] = value["run_type"] if value["run_type"] in SAFE_VALUES else "chain"
    if value.get("error"):
        result["error"] = "redacted_run_error"
    if "extra" in value:
        result["extra"] = {"metadata": safe_content((value["extra"] or {}).get("metadata", {}))}
    # Attachments, event bodies, serialized prompts, tags and all unknown fields are omitted.
    return result


class SafeTraceClient(Client):
    def _run_transform(self, run, update=False, copy=False):
        return super()._run_transform(safe_run(run), update=update, copy=copy)

    def update_run(self, run_id, **kwargs):
        return super().update_run(run_id, **safe_run(kwargs))


def trace_client(*, api_url, api_key, **kwargs):
    os.environ["LANGSMITH_TRACING_QUEUE_MAX_SIZE"] = "64"
    return SafeTraceClient(
        api_url=api_url,
        api_key=api_key,
        auto_batch_tracing=True,
        omit_traced_runtime_info=True,
        hide_inputs=safe_content,
        hide_outputs=safe_content,
        hide_metadata=safe_content,
        timeout_ms=(1000, 1500),
        retry_config=Retry(total=0),
        **kwargs,
    )
