"""Run from a fixed legacy checkout. No edits/imports from the new application's code."""

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path.cwd()))


def main():
    # This standalone worker runs from the archived old checkout. Its script directory
    # remains on sys.path, so the evaluator helper comes from the current pinned runner.
    from langchain_core.callbacks import BaseCallbackHandler
    from legacy_config import aligned_config, public_call_config
    from legacy_scoring import score_legacy
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
        Offer,
        Product,
    )
    from app.services.approval_service import ApprovalService
    from app.services.chat_service import BuyerOfferSubmission, ChatService
    from app.services.intent_service import IntentService
    from app.services.pricing_service import OfferTerms, ShippingPayer
    from app.workers.approval_processor import ApprovalProcessor
    from tests.integration.factories import create_negotiation

    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    factory = sessionmaker(engine, expire_on_commit=False)
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    output = Path(sys.argv[2])
    if output.exists():
        raise RuntimeError("refusing to overwrite old-version evidence")
    settings = get_settings()
    lock = threading.Lock()

    @dataclass(frozen=True, slots=True)
    class SourceSubmission(BuyerOfferSubmission):
        """Map the full frozen fixture into the original service's terms interface."""

        source_discount: Decimal = Decimal("0")
        source_terms: dict | None = None

        def to_terms(self):
            return OfferTerms(
                self.price, self.shipping_paid_by, self.shipping_cost, self.source_discount
            )

        def additional_terms(self):
            return dict(self.source_terms or {})

    class Recorder(BaseCallbackHandler):
        def __init__(self, calls):
            self.calls = calls
            self.configs = {}

        def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
            self.configs[str(run_id)] = public_call_config(
                serialized, kwargs.get("invocation_params", {})
            )

        def on_llm_end(self, response, *, run_id, **kwargs):
            usage = response.generations[0][0].message.usage_metadata or {}
            inp, out = usage.get("input_tokens"), usage.get("output_tokens")
            cost = None
            if inp is not None and out is not None and settings.model_cost_is_configured:
                cost = (
                    Decimal(inp) * settings.model_input_price_per_million
                    + Decimal(out) * settings.model_output_price_per_million
                ) / 1000000
            self.calls.append(
                {
                    "status": "OK",
                    "tokens": usage.get("total_tokens"),
                    "estimated_cny": str(cost) if cost is not None else None,
                    "model_config": self.configs.pop(str(run_id), None),
                }
            )

        def on_llm_error(self, error, *, run_id, **kwargs):
            usage = getattr(getattr(error, "completion", None), "usage", None)
            inp, out = (
                getattr(usage, "prompt_tokens", None),
                getattr(usage, "completion_tokens", None),
            )
            cost = (
                (
                    Decimal(inp) * settings.model_input_price_per_million
                    + Decimal(out) * settings.model_output_price_per_million
                )
                / 1000000
                if inp is not None and out is not None and settings.model_cost_is_configured
                else None
            )
            self.calls.append(
                {
                    "status": "ERROR",
                    "tokens": getattr(usage, "total_tokens", None),
                    "estimated_cny": str(cost) if cost is not None else None,
                    "error_type": type(error).__name__,
                    "model_config": self.configs.pop(str(run_id), None),
                }
            )

    class AlignedDecisionProvider(LangChainDecisionProvider):
        def invoke_agent(self, messages, tools, config, *, purpose="REACT"):
            return super().invoke_agent(messages, tools, aligned_config(config), purpose=purpose)

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
            "recorded_calls": [],
        }
        model = (
            QwenChatModelFactory()
            .create(settings)
            .model_copy(
                update={
                    "max_tokens": 2048,
                    "temperature": 0,
                    "request_timeout": 45,
                    "max_retries": 0,
                    "extra_body": {"enable_thinking": False},
                }
            )
        )
        model.callbacks = [Recorder(row["recorded_calls"])]
        policy = case.get(
            "policy", {"minimum_net_price": "750", "auto_accept_threshold": "850", "max_rounds": 6}
        )
        sid, buyer = create_negotiation(
            factory,
            minimum_net_price=Decimal(policy["minimum_net_price"]),
            auto_accept_threshold=Decimal(policy["auto_accept_threshold"]),
        )
        with factory() as db, db.begin():
            product = db.get(Product, db.get(NegotiationSession, sid).product_id)
            data = case.get(
                "product",
                dict(
                    title="P5二手手机",
                    description="电池健康92%；维修情况未知；支持自提与快递。",
                    listed_price="1000",
                ),
            )
            product.title, product.description, product.listed_price = (
                data["title"],
                data["description"],
                Decimal(data["listed_price"]),
            )
            product.policy.max_rounds = policy["max_rounds"]
            seller = product.seller_id
        chat = ChatService(factory, AlignedDecisionProvider(model))
        try:
            turns = list(case["turns"])
            if case.get("final_action"):
                turns.append("确认" if case["final_action"] == "CONFIRM" else "结束协商")
            for index, text in enumerate(turns):
                start = time.monotonic()
                mode = case.get("mode")
                final_action = case.get("final_action") if index == len(case["turns"]) else None
                if final_action == "CONFIRM" or (index and mode in {"approve", "confirm"}):
                    with factory() as db:
                        oid = db.get(NegotiationSession, sid).current_offer_id
                    IntentService(factory).confirm_offer(
                        session_id=sid, buyer_id=buyer, offer_id=oid, request_id=uuid4().hex
                    )
                elif final_action == "CLOSE" or (index and mode == "close"):
                    IntentService(factory).close_negotiation(
                        session_id=sid, buyer_id=buyer, request_id=uuid4().hex
                    )
                else:
                    source = (
                        case.get("scripted_offers", [])[index]
                        if case.get("source_scenario")
                        else None
                    )
                    submission = None
                    if source:
                        submission = SourceSubmission(
                            price=Decimal(source["price"]),
                            shipping_paid_by=ShippingPayer(source["shipping_paid_by"]),
                            shipping_cost=Decimal(source["shipping_cost"])
                            if source["shipping_cost"] is not None
                            else None,
                            source_discount=Decimal(source["seller_borne_discount"]),
                            source_terms={k: str(v) for k, v in source["additional_terms"].items()},
                        )
                    elif not case.get("source_scenario"):
                        submission = BuyerOfferSubmission(
                            price=Decimal(case["amount"]),
                            shipping_paid_by=ShippingPayer.BUYER,
                            delivery_method="pickup",
                        )
                    result = chat.send_buyer_message(
                        session_id=sid,
                        buyer_id=buyer,
                        request_id="replay-request" if mode == "replay" else uuid4().hex,
                        content=text,
                        offer=submission,
                    )
                    row["runs"].append(
                        {"outcome": result.outcome, "reply": result.agent_message.content}
                    )
                review = case.get("approval") if index == 0 else None
                if case.get("seller_review"):
                    with factory() as db:
                        pending = db.scalar(
                            select(ApprovalRequest).where(
                                ApprovalRequest.session_id == sid,
                                ApprovalRequest.status == "PENDING",
                            )
                        )
                    review = case["seller_review"] if pending else None
                if review:
                    service = ApprovalService(factory)
                    with factory() as db:
                        approval = db.scalar(
                            select(ApprovalRequest).where(
                                ApprovalRequest.session_id == sid,
                                ApprovalRequest.status == "PENDING",
                            )
                        )
                        aid = approval.id
                    if review == "expire":
                        with factory() as db, db.begin():
                            db.get(ApprovalRequest, aid).expires_at = datetime.now() - timedelta(1)
                        service.expire_request(approval_id=aid)
                    else:
                        method = (
                            service.approve_request
                            if review == "approve"
                            else service.reject_request
                        )
                        method(seller_id=seller, approval_id=aid, request_id=uuid4().hex)
                    with factory() as db:
                        other_ids = set(
                            db.scalars(select(ApprovalRequest.id).where(ApprovalRequest.id != aid))
                        )
                    ApprovalProcessor(
                        factory, LangChainApprovalFollowupProvider(model)
                    ).process_next(excluded_approval_ids=other_ids)
                row["latency_seconds"].append(time.monotonic() - start)
        except Exception as exc:
            row.update(status="ERROR", error_type=type(exc).__name__)
            if getattr(exc, "orig", None) and exc.orig.args:
                row["database_error_code"] = exc.orig.args[0]
        with factory() as db:
            session = db.get(NegotiationSession, sid)
            row.update(
                session_status=session.status.value,
                rounds=session.round_count,
                database_evidence=True,
                request_count=len(case["turns"]),
            )
            row["messages"] = [
                m.content for m in db.scalars(select(Message).where(Message.session_id == sid))
            ]
            row["offers"] = [
                {
                    "id": o.id,
                    "status": o.status.value,
                    "amount": str(o.price),
                    "shipping_paid_by": o.shipping_paid_by.value,
                    "shipping_cost": str(o.shipping_cost) if o.shipping_cost is not None else None,
                    "seller_discount": str(o.seller_borne_discount),
                    "terms": o.terms,
                }
                for o in db.scalars(select(Offer).where(Offer.session_id == sid))
            ]
            row["approvals"] = [
                {"status": a.status.value}
                for a in db.scalars(
                    select(ApprovalRequest).where(ApprovalRequest.session_id == sid)
                )
            ]
            row["confirmed_offer_id"] = session.confirmed_offer_id
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
            row["business_pass"] = score_legacy(case, row)
        with lock, output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"legacy {case['case_id']} repeat={repeat} status={row['status']}", flush=True)

    with ThreadPoolExecutor(max_workers=int(os.environ.get("P5_CONCURRENCY", "1"))) as pool:
        list(pool.map(execute, spec))
    engine.dispose()


if __name__ == "__main__":
    main()
