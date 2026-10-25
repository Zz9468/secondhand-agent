"""Only the strict export client may enable native tracing in a business runner."""

from contextlib import contextmanager
from functools import lru_cache

from langsmith import tracing_context
from langsmith.run_helpers import get_tracing_context
from langsmith.wrappers import wrap_openai

from app.core.config import get_settings
from app.p0.tracing import SafeTraceClient, trace_client


def traced_openai(client):
    return wrap_openai(client) if get_tracing_context().get("enabled") else client


@lru_cache(maxsize=1)
def configured_client():
    settings = get_settings()
    if not settings.graph_langsmith_tracing or not settings.langsmith_api_key:
        return None
    return trace_client(
        api_url=settings.langsmith_endpoint, api_key=settings.langsmith_api_key.get_secret_value()
    )


@contextmanager
def graph_trace():
    parent = get_tracing_context()
    inherited = parent.get("enabled") and isinstance(parent.get("client"), SafeTraceClient)
    client = parent["client"] if inherited else configured_client()
    with tracing_context(
        enabled=client is not None,
        client=client,
        project_name=parent.get("project_name") if inherited else "secondhand-agent-langgraph-dev",
    ):
        yield
