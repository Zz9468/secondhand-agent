import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TypedDict
from uuid import uuid4

import httpx
import pytest
from langgraph.graph import END, START, StateGraph
from langsmith import traceable, tracing_context
from langsmith.wrappers import wrap_openai
from openai import OpenAI

from app.p0.tracing import trace_client

MARKER = "SYNTHETIC_PRIVATE_FLOOR_TOKEN_ADDRESS_98137"


@pytest.fixture
def receiver():
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            data = json.dumps(
                {
                    "batch_ingest_config": {
                        "use_multipart_endpoint": False,
                        "size_limit": 100,
                        "scale_up_nthreads_limit": 1,
                        "scale_up_qsize_trigger": 1000,
                        "scale_down_nempty_trigger": 4,
                    }
                }
            ).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            payloads.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        do_PATCH = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", payloads
    server.shutdown()
    server.server_close()


def exercise_native_traces(client, project):
    class State(TypedDict):
        private: str

    builder = StateGraph(State)
    builder.add_node("query_product", lambda state: {"private": state["private"]})
    builder.add_edge(START, "query_product").add_edge("query_product", END)
    # No checkpoint required: this test inspects native tracing export, not persistence.
    graph = builder.compile()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen3.7-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": MARKER},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )
    )
    sdk = wrap_openai(
        OpenAI(api_key="synthetic-only", http_client=httpx.Client(transport=transport))
    )

    @traceable(name="p0_trace", client=client)
    def root(private):
        graph.invoke({"private": private}, {"metadata": {"private_policy": MARKER}})
        sdk.chat.completions.create(
            model="qwen3.7-flash",
            messages=[{"role": "user", "content": MARKER}],
            langsmith_extra={"client": client},
        )
        return {"private": private}

    @traceable(name="p0_error", client=client)
    def error():
        raise ValueError(MARKER)

    with tracing_context(enabled=True, client=client, project_name=project):
        root(MARKER)
        with pytest.raises(ValueError):
            error()
        attachment = uuid4()
        client.create_run(
            name="p0_attachment",
            inputs={"secret": MARKER},
            run_type="chain",
            id=attachment,
            project_name=project,
            attachments={MARKER: ("text/plain", MARKER.encode())},
            extra={"metadata": {"confirmation_token": MARKER}},
        )
        client.update_run(
            attachment,
            outputs={"private": MARKER},
            error=MARKER,
            events=[{"name": MARKER}],
            tags=[MARKER],
            attachments={"payload": ("text/plain", MARKER.encode())},
        )
    client.flush(timeout=5)


def test_native_root_node_sdk_error_and_attachment_boundaries(receiver):
    url, payloads = receiver
    client = trace_client(api_url=url, api_key="synthetic-only")
    exercise_native_traces(client, "secondhand-agent-langgraph-test")
    wire = b"\n".join(payloads)
    assert payloads and MARKER.encode() not in wire
    for name in (b"p0_trace", b"query_product", b"p0_error", b"p0_attachment"):
        assert name in wire
    assert b'"llm"' in wire
    assert b"redacted_run_error" in wire
    assert b"attachments" not in wire
    assert client.tracing_queue.maxsize == 64


def test_export_failure_does_not_block_caller():
    client = trace_client(
        api_url="http://127.0.0.1:1",
        api_key="synthetic-only",
        info={
            "batch_ingest_config": {
                "use_multipart_endpoint": False,
                "size_limit": 100,
                "scale_up_nthreads_limit": 1,
                "scale_up_qsize_trigger": 1000,
                "scale_down_nempty_trigger": 4,
            }
        },
    )
    start = time.monotonic()
    run_id = uuid4()
    client.create_run(
        "p0_trace",
        {},
        "chain",
        id=run_id,
        trace_id=run_id,
        dotted_order=f"20261024T000000000000Z{run_id}",
    )
    assert time.monotonic() - start < 0.5
    client.flush(timeout=2)
