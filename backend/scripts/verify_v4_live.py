"""Local HTTP/MySQL/Worker verification with isolated synthetic accounts and products."""

import argparse
import json
import secrets
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.db.models import AgentCallSnapshot, AgentMessage, Message, MessageRole, ModelExecutionTask
from app.db.session import get_session_factory
from app.services.agent_message_service import AgentMessageService
from scripts.evaluate_live_workflow import ApiClient, _wait_followup


def prepare(output, base_url):
    suffix = uuid4().hex[:12]
    seller, buyer = ApiClient(base_url), ApiClient(base_url)
    identities = []
    for role, client in (("seller", seller), ("buyer", buyer)):
        username = f"v4check-{role}-{suffix}"
        password = secrets.token_urlsafe(24) + "-9!"
        _, identity = client.request(
            "POST",
            "/api/auth/register",
            {"username": username, "display_name": "V4 隔离验证" + role, "password": password},
            expected_status=201,
        )
        identities.append(identity["id"])
        # Test credentials remain in the ignored result directory, never stdout or reports.
        output.mkdir(parents=True, exist_ok=True)
        (output / f"{role}-credentials.json").write_text(
            json.dumps({"username": username, "password": password}), encoding="utf-8"
        )
    _, product = seller.request(
        "POST",
        "/api/products",
        {
            "title": "V4 验证用手机",
            "description": "九五新，电池健康度89%，配件齐全，无维修记录。",
            "listed_price": "3000",
            "status": "AVAILABLE",
            "policy": {
                "minimum_net_price": "2700",
                "auto_accept_threshold": "2850",
                "max_rounds": 6,
            },
        },
        expected_status=201,
    )
    _, session = buyer.request("POST", "/api/negotiations", {"product_id": product["id"]})
    data = {
        "base_url": base_url,
        "session_id": session["session_id"],
        "product_id": product["id"],
        "account_ids": identities,
        "synthetic_data": True,
    }
    (output / "setup.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    with get_session_factory()() as db, db.begin():
        for number in range(22):
            message = Message(
                session_id=data["session_id"],
                request_id=f"history-{suffix}-{number}",
                role=MessageRole.BUYER if number % 2 == 0 else MessageRole.AGENT,
                content="我可以自提。"
                if number == 0
                else (f"补充咨询 {number}" if number % 2 == 0 else "已记录咨询。"),
            )
            db.add(message)
            db.flush()
            AgentMessageService.project(db, message)
    print(json.dumps(data, ensure_ascii=False))


def verify(output):
    setup = json.loads((output / "setup.json").read_text(encoding="utf-8"))
    clients = {}
    for role in ("seller", "buyer"):
        client = ApiClient(setup["base_url"])
        credentials = json.loads((output / f"{role}-credentials.json").read_text(encoding="utf-8"))
        client.request("POST", "/api/auth/login", credentials)
        clients[role] = client
    buyer, seller = clients["buyer"], clients["seller"]
    sid = setup["session_id"]
    checks = []
    payload = {"request_id": "v4-live-inquiry", "content": "我可以自提，请介绍一下商品成色。"}
    _, result = buyer.request(
        "POST", f"/api/negotiations/{sid}/messages", payload, expected_status=201
    )
    assert result["outcome"] in {"INFORMATIONAL", "CLARIFICATION"}
    _, replay = buyer.request(
        "POST", f"/api/negotiations/{sid}/messages", payload, expected_status=201
    )
    assert (
        replay["idempotent_replay"]
        and replay["agent_message"]["id"] == result["agent_message"]["id"]
    )
    checks.append("native_runtime_and_idempotent_http_replay")
    _, messages = buyer.request("GET", f"/api/negotiations/{sid}/messages")
    assert len(messages["messages"]) >= 24
    assert sum(m["request_id"] == payload["request_id"] for m in messages["messages"]) == 1
    assert all(
        set(m) == {"id", "role", "content", "request_id", "created_at"}
        for m in messages["messages"]
    )
    checks.append("public_projection_contains_no_internal_payloads")
    with get_session_factory()() as db:
        task = db.scalar(
            select(ModelExecutionTask).where(
                ModelExecutionTask.session_id == sid, ModelExecutionTask.runtime_kind == "V4"
            )
        )
        assert task and task.status.value == "SUCCEEDED"
        calls = list(
            db.scalars(select(AgentCallSnapshot).where(AgentCallSnapshot.model_task_id == task.id))
        )
        react = next(c for c in calls if c.purpose == "REACT")
        assert (
            1 in react.context_manifest["source_seqs"]
            or 1 in react.context_manifest["summary_source_seqs"]
        )
        assert task.input_snapshot["agent_config"]["tools"] == react.request_payload["tools"]
        task_id = task.id
    checks.append("history_beyond_twenty_and_frozen_native_tool_schema")
    _, submitted = buyer.request(
        "POST",
        f"/api/negotiations/{sid}/messages",
        {
            "request_id": "v4-live-approval",
            "content": "正式报价2800元，运费由我承担，请卖家确认。",
            "offer": {
                "price": "2800",
                "shipping_paid_by": "buyer",
                "shipping_cost": "0",
                "delivery_method": "pickup",
            },
        },
        expected_status=201,
    )
    assert submitted["outcome"] == "NEEDS_SELLER_CONFIRMATION"
    _, approvals = seller.request("GET", "/api/seller/approvals?limit=500")
    approval = next(a for a in approvals["approvals"] if a["session_id"] == sid)
    seller.request(
        "POST",
        f"/api/seller/approvals/{approval['id']}/approve",
        {"request_id": f"v4-live-review-{sid}", "comment": "同意验证用条件"},
    )
    followup = _wait_followup(seller, approval_id=approval["id"], timeout_seconds=45)
    assert followup["followup_status"] == "SENT"
    _, confirmed = buyer.request(
        "POST",
        f"/api/negotiations/{sid}/confirm",
        {"request_id": f"v4-live-confirm-{sid}", "offer_id": followup["offer_id"]},
    )
    assert confirmed["status"] == "AGREED"
    checks.append("worker_approval_notification_and_buyer_confirmation")
    with get_session_factory()() as db:
        public = list(db.scalars(select(Message).where(Message.session_id == sid)))
        assert all(m.source_agent_message_id for m in public)
        stream = list(db.scalars(select(AgentMessage).where(AgentMessage.session_id == sid)))
        assert any(r.kind == "APPROVAL_NOTIFICATION" for r in stream)
        assert any(r.kind == "SESSION_NOTIFICATION" for r in stream)
        tasks = list(
            db.scalars(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        )
        primary_reserved = sum(
            Decimal(t.runtime_checkpoint.get("reserved_cost", "0"))
            for t in tasks
            if t.runtime_checkpoint
        )
        known_cost = sum(t.estimated_cost for t in tasks if t.estimated_cost is not None)
        usage_unknown = sum(t.total_tokens is None for t in tasks)
    report = {
        "status": "PASS",
        "checks": checks,
        "session_id": sid,
        "native_task_id": task_id,
        "primary_reserved_cost": str(primary_reserved),
        "known_estimated_cost": str(known_cost),
        "unknown_task_usage": usage_unknown,
        "synthetic_data": True,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "verify"])
    parser.add_argument("--output", type=Path, default=Path("evaluation/results/v4-live-final-01"))
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    prepare(args.output, args.api_url) if args.phase == "prepare" else verify(args.output)


if __name__ == "__main__":
    main()
