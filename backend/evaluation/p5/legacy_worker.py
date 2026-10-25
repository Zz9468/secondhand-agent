"""Run from a fixed legacy checkout. No edits/imports from the new application's code."""

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path.cwd()))


def main():
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.agent.approval_followup import LangChainApprovalFollowupProvider
    from app.agent.decision_provider import LangChainDecisionProvider
    from app.agent.model_factory import QwenChatModelFactory
    from app.core.config import get_settings
    from app.db.models import (
        AgentCallSnapshot,
        ApprovalRequest,
        Message,
        ModelExecutionTask,
        NegotiationSession,
        Product,
    )
    from app.services.approval_service import ApprovalService
    from app.services.chat_service import BuyerOfferSubmission, ChatService
    from app.services.intent_service import IntentService
    from app.services.pricing_service import ShippingPayer
    from app.workers.approval_processor import ApprovalProcessor
    from tests.integration.factories import create_negotiation

    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    factory = sessionmaker(engine, expire_on_commit=False)
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    output = Path(sys.argv[2])
    if output.exists():
        raise RuntimeError("refusing to overwrite old-version evidence")
    settings = get_settings()
    model = QwenChatModelFactory().create(settings)
    lock = threading.Lock()

    def execute(item):
        case, repeat = item["case"], item["repeat"]
        row = {
            "case_id": case["case_id"],
            "repeat": repeat,
            "target": "legacy",
            "status": "OK",
            "runs": [],
            "latency_seconds": [],
            "auxiliary_calls": [],
        }
        sid, buyer = create_negotiation(
            factory, minimum_net_price=Decimal("750"), auto_accept_threshold=Decimal("850")
        )
        with factory() as db, db.begin():
            product = db.get(Product, db.get(NegotiationSession, sid).product_id)
            product.title, product.description, product.listed_price = (
                "P5二手手机",
                "电池健康92%；维修情况未知；支持自提与快递。",
                Decimal("1000"),
            )
            seller = product.seller_id
        chat = ChatService(factory, LangChainDecisionProvider(model))
        try:
            for index, text in enumerate(case["turns"]):
                start = time.monotonic()
                mode = case.get("mode")
                if index and mode in {"approve", "confirm"}:
                    with factory() as db:
                        oid = db.get(NegotiationSession, sid).current_offer_id
                    IntentService(factory).confirm_offer(
                        session_id=sid, buyer_id=buyer, offer_id=oid, request_id=uuid4().hex
                    )
                elif index and mode == "close":
                    IntentService(factory).close_negotiation(
                        session_id=sid, buyer_id=buyer, request_id=uuid4().hex
                    )
                else:
                    result = chat.send_buyer_message(
                        session_id=sid,
                        buyer_id=buyer,
                        request_id="replay-request" if mode == "replay" else uuid4().hex,
                        content=text,
                        offer=BuyerOfferSubmission(
                            price=Decimal(case["amount"]),
                            shipping_paid_by=ShippingPayer.BUYER,
                            delivery_method="pickup",
                        ),
                    )
                    row["runs"].append(
                        {"outcome": result.outcome, "reply": result.agent_message.content}
                    )
                if index == 0 and case.get("approval"):
                    service = ApprovalService(factory)
                    with factory() as db:
                        approval = db.scalar(
                            select(ApprovalRequest).where(ApprovalRequest.session_id == sid)
                        )
                        aid = approval.id
                    if case["approval"] == "expire":
                        with factory() as db, db.begin():
                            db.get(ApprovalRequest, aid).expires_at = datetime.now() - timedelta(1)
                        service.expire_due_requests()
                    else:
                        method = (
                            service.approve_request
                            if case["approval"] == "approve"
                            else service.reject_request
                        )
                        method(seller_id=seller, approval_id=aid, request_id=uuid4().hex)
                    ApprovalProcessor(
                        factory, LangChainApprovalFollowupProvider(model)
                    ).process_batch()
                row["latency_seconds"].append(time.monotonic() - start)
        except Exception as exc:
            row.update(status="ERROR", error_type=type(exc).__name__)
        with factory() as db:
            session = db.get(NegotiationSession, sid)
            row.update(
                session_status=session.status.value,
                rounds=session.round_count,
                database_evidence=True,
                request_count=len(case["turns"]),
                business_pass=row["status"] == "OK"
                and session.status.value == case["expected_status"],
            )
            row["messages"] = [
                m.content for m in db.scalars(select(Message).where(Message.session_id == sid))
            ]
            tasks = list(
                db.scalars(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
            )
            row["tasks"] = [
                {
                    "status": t.status.value,
                    "runtime_kind": t.runtime_kind,
                    "tokens": t.total_tokens,
                    "estimated_cny": str(t.estimated_cost)
                    if t.estimated_cost is not None
                    else None,
                }
                for t in tasks
            ]
            row["calls"] = [
                {"purpose": c.purpose, "status": c.status, "usage": c.usage_json}
                for c in db.scalars(
                    select(AgentCallSnapshot).where(
                        AgentCallSnapshot.model_task_id.in_([t.id for t in tasks])
                    )
                )
            ]
            for index, expected in enumerate(case.get("expected_outcomes", [])):
                actual = row["runs"][index]["outcome"] if index < len(row["runs"]) else None
                mapped = {"NEEDS_SELLER_CONFIRMATION": "WAITING_APPROVAL"}.get(actual, actual)
                if mapped != expected:
                    row["business_pass"] = False
        with lock, output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"legacy {case['case_id']} repeat={repeat} status={row['status']}", flush=True)
    with ThreadPoolExecutor(max_workers=int(os.environ.get("P5_CONCURRENCY", "1"))) as pool:
        list(pool.map(execute, spec))
    engine.dispose()


if __name__ == "__main__":
    main()
