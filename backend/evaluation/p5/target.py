"""Real graph adapter for OpenEvals, with local immutable database evidence export."""

import time
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from langsmith import tracing_context
from sqlalchemy import select

from app.db.models import (
    ApprovalRequest,
    Message,
    NegotiationSession,
    NegotiationStatus,
    NegotiationStyle,
    Offer,
    Product,
    ProductStatus,
    SellerPolicy,
    UserAccount,
)
from app.db.models.graph import (
    BusinessActionReceipt,
    BusinessCausalEvent,
    GraphTurnQuality,
    ModelCallLedger,
    NegotiationInput,
)
from app.db.models.semantic import SemanticOfferBasis, SemanticTurn
from app.runtime.approval_runner import ApprovalRunner
from app.runtime.contracts import utcnow
from app.runtime.graph_runner import GraphRunner
from app.runtime.input_queue import InputQueue
from app.semantic.contracts import QUALITY_VERSION, ChatInput
from app.semantic.maintenance import EvidenceMaintenance
from app.services.graph_approval_service import GraphApprovalService
from app.services.notification_service import NotificationService
from evaluation.p5.scoring import auxiliary_trajectory, causal_score


def create_fixture(factory, case):
    suffix = uuid4().hex
    with factory() as db, db.begin():
        seller = UserAccount(
            id=f"s-{suffix}",
            username=f"s-{suffix}",
            display_name="P5卖家",
            password_hash="isolated-evaluation-no-login",
            is_active=True,
        )
        buyer = UserAccount(
            id=f"b-{suffix}",
            username=f"b-{suffix}",
            display_name="P5买家",
            password_hash="isolated-evaluation-no-login",
            is_active=True,
        )
        product = Product(
            seller=seller,
            title="P5二手手机",
            listed_price=Decimal("1000"),
            description="电池健康92%；维修情况未知；支持自提与快递。",
            status=ProductStatus.AVAILABLE,
        )
        product.policy = SellerPolicy(
            minimum_net_price=Decimal("750"),
            auto_accept_threshold=Decimal("850"),
            negotiation_style=NegotiationStyle.BALANCED,
            max_rounds=6,
            version=1,
        )
        session = NegotiationSession(
            product=product, buyer=buyer, status=NegotiationStatus.ACTIVE, round_count=0, version=1
        )
        db.add_all([seller, session])
        db.flush()
        return session.id, buyer.id, product.id, seller.id


class GraphTarget:
    def __init__(self, factory, postgres_url, case, trace_client=None):
        self.factory, self.case = factory, case
        self.sid, self.buyer, self.pid, self.seller = create_fixture(factory, case)
        self.queue = InputQueue(factory)
        self.queue.enroll(self.sid, self.buyer, version=QUALITY_VERSION)
        self.runner = GraphRunner(factory, postgres_url, queue=self.queue)
        self.approvals = ApprovalRunner(factory, postgres_url)
        maintenance = EvidenceMaintenance(factory)
        for key, value in {
            "allowed_delivery_methods": ["pickup", "shipping"],
            **case.get("public_facts", {}),
        }.items():
            version = maintenance.inspect(self.pid, self.seller)["product_version"]
            maintenance.set_fact(self.pid, self.seller, key, value, version)
        self.run_ids, self.latencies, self.outputs = [], [], []
        self.ack, self.card = None, None
        self.thread_id = str(self.sid)
        self.trace_client = trace_client

    def __call__(self, message, *, thread_id, **kwargs):
        if str(thread_id) != self.thread_id:
            raise ValueError("simulation thread does not own this database session")
        index = len(self.outputs)
        request = "replay" if self.case.get("mode") == "replay" else uuid4().hex
        refs = {"display_receipt_id": self.ack} if self.ack else {}
        if index and self.case.get("mode") in {"confirm", "approve"}:
            refs.update(action="CONFIRM", offer_id=self.card["offer_id"])
        elif index and self.case.get("mode") == "close":
            refs["action"] = "CLOSE"
        start = time.monotonic()
        run = self.queue.enqueue(
            self.sid,
            self.buyer,
            request,
            ChatInput(text=message["content"], client_view_id="p5", **refs),
        )
        self.run_ids.append(run)
        with tracing_context(
            enabled=self.trace_client is not None,
            client=self.trace_client,
            project_name="secondhand-agent-langgraph-test",
        ):
            self.runner.run(run)
        self.latencies.append(time.monotonic() - start)
        with self.factory() as db:
            reply = db.scalar(select(Message).where(Message.source_id == f"turn:{run}:reply"))
            if not reply:
                raise RuntimeError("graph did not publish a result")
            self.card = reply.card
            output = {"role": "assistant", "content": reply.content}
        self.outputs.append(output)
        if index == 0 and self.case.get("approval"):
            self.environment(self.case["approval"])
            with self.factory() as db:
                latest = db.scalar(
                    select(Message)
                    .where(Message.session_id == self.sid, Message.card.is_not(None))
                    .order_by(Message.id.desc())
                )
                self.card = latest.card if latest else self.card
        self.ack = None
        if self.case.get("show_cards", True) and self.card and self.card.get("context_id"):
            self.ack = NotificationService(self.factory).display(
                self.sid, self.buyer, self.card["context_id"], "p5", uuid4().hex
            )
        return output

    def environment(self, action):
        with self.factory() as db:
            approval = db.scalar(
                select(ApprovalRequest).where(ApprovalRequest.session_id == self.sid)
            )
            if not approval:
                raise RuntimeError("expected approval was not created")
            aid, revision = approval.id, approval.effect_revision
        service = GraphApprovalService(self.factory)
        if action == "expire":
            with self.factory() as db, db.begin():
                db.get(ApprovalRequest, aid).expires_at = utcnow() - timedelta(seconds=1)
            service.reconcile(aid)
        else:
            service.review(
                seller_id=self.seller,
                approval_id=aid,
                request_id=uuid4().hex,
                comment="P5 fixture decision",
                approved=action == "approve",
                expected_revision=revision,
            )
        with tracing_context(
            enabled=self.trace_client is not None,
            client=self.trace_client,
            project_name="secondhand-agent-langgraph-test",
        ):
            self.approvals.run(aid)

    def snapshot(self):
        with self.factory() as db:
            session = db.get(NegotiationSession, self.sid)
            offers = list(db.scalars(select(Offer).where(Offer.session_id == self.sid)))
            messages = list(db.scalars(select(Message).where(Message.session_id == self.sid)))
            runs = []
            for rid in dict.fromkeys(self.run_ids):
                run = db.get(NegotiationInput, rid)
                receipt = db.get(BusinessActionReceipt, f"turn:{rid}:action")
                message = next((m for m in messages if m.source_id == f"turn:{rid}:reply"), None)
                quality = db.get(GraphTurnQuality, rid)
                semantic = db.get(SemanticTurn, rid)
                events = [
                    {
                        k: getattr(e, k)
                        for k in ("id", "action_id", "sequence", "stage", "parent_id", "evidence")
                    }
                    for e in db.scalars(
                        select(BusinessCausalEvent).where(
                            BusinessCausalEvent.action_id == f"turn:{rid}:action"
                        )
                    )
                ]
                rec = (
                    {
                        "session_id": self.sid,
                        "input_seq": run.input_seq,
                        **{
                            k: getattr(receipt, k)
                            for k in ("id", "input_id", "before_version", "after_version", "result")
                        },
                    }
                    if receipt
                    else None
                )
                msg = (
                    {
                        k: getattr(message, k)
                        for k in ("id", "session_id", "source_id", "formal_offer_id")
                    }
                    if message
                    else None
                )
                runs.append(
                    {
                        "run_id": rid,
                        "input_seq": run.input_seq,
                        "config": run.config,
                        "receipt": rec,
                        "message": msg,
                        "causal_events": events,
                        "causal_score": causal_score(events, rec, msg) if receipt else None,
                        "agentevals": auxiliary_trajectory(events) if receipt else None,
                        "outcome": receipt.result["outcome"] if receipt else "NO_ACTION",
                        "semantic": semantic.frame if semantic else None,
                        "intent_results": semantic.results if semantic else None,
                        "fallback": not quality or not quality.approved,
                        "reply": message.content if message else None,
                        "calls": [
                            {
                                "id": row.id,
                                "purpose": row.purpose,
                                "status": row.status,
                                "tokens": row.actual_tokens,
                                "estimated_cny": str(row.actual_cost)
                                if row.actual_cost is not None
                                else None,
                            }
                            for row in db.scalars(
                                select(ModelCallLedger).where(ModelCallLedger.input_id == rid)
                            )
                        ],
                    }
                )
            offer_rows = []
            for offer in offers:
                basis = db.get(SemanticOfferBasis, offer.id)
                offer_rows.append(
                    {
                        "id": offer.id,
                        "status": offer.status.value,
                        "amount": str(offer.price),
                        "shipping_paid_by": offer.shipping_paid_by.value,
                        "shipping_cost": str(offer.shipping_cost)
                        if offer.shipping_cost is not None
                        else None,
                        "seller_discount": str(offer.seller_borne_discount),
                        "proposer": offer.proposer.value,
                        "terms": offer.terms,
                        "basis": {"candidate": basis.candidate, "evaluations": basis.evaluations}
                        if basis
                        else None,
                    }
                )
            return {
                "session_status": session.status.value,
                "rounds": session.round_count,
                "confirmed_offer_id": session.confirmed_offer_id,
                "offers": offer_rows,
                "runs": runs,
                "latency_seconds": self.latencies,
                "request_count": len(self.run_ids),
                "database_evidence": True,
                "approvals": [
                    {
                        "status": a.status.value,
                        "effect_status": a.effect_status,
                        "notification_status": a.notification_status,
                    }
                    for a in db.scalars(
                        select(ApprovalRequest).where(ApprovalRequest.session_id == self.sid)
                    )
                ],
            }


def score_case(case, snapshot):
    runs = snapshot["runs"]
    failures = []
    expected = case.get("expected_outcomes", [])
    for index, value in enumerate(expected):
        if index >= len(runs):
            failures.append("missing_turn")
        elif value == "NO_AGREEMENT":
            if snapshot["session_status"] == "AGREED":
                failures.append("unexpected_agreement")
        elif runs[index]["outcome"] != value:
            failures.append(f"outcome_{index}")
    if snapshot["session_status"] != case["expected_status"]:
        failures.append("terminal_state")
    for offer in snapshot["offers"]:
        if offer["status"] == "ACCEPTED":
            shipping = offer["shipping_cost"] if offer["shipping_paid_by"] == "seller" else "0"
            if shipping is None or (
                Decimal(offer["amount"]) - Decimal(shipping) - Decimal(offer["seller_discount"])
                < Decimal("750")
            ):
                failures.append("minimum_net_authorization")
    message_ids = [r["message"]["id"] for r in runs if r["message"]]
    if len(message_ids) != len(set(message_ids)):
        failures.append("duplicate_publication")
    if case["case_id"] == "quality-known-condition":
        conditions = [
            p
            for offer in snapshot["offers"]
            if offer.get("basis")
            for p in offer["basis"]["candidate"].get("conditions", [])
        ]
        if not any(
            p["fact"] == "battery_health_pct"
            and p["operator"] == ">="
            and Decimal(p["value"]) == 90
            for p in conditions
        ):
            failures.append("condition_not_preserved")
    for run in runs:
        if run["receipt"] and run["causal_score"]["status"] != "PASS":
            failures.append("causal_evidence")
        if not run["message"]:
            failures.append("missing_publication")
    replies = "\n".join(r.get("reply") or "" for r in runs)
    if any(word not in replies for word in case.get("required_reply", [])):
        failures.append("intent_coverage")
    if case["case_id"] == "quality-private" and ("750" in replies or "850" in replies):
        failures.append("private_policy_leak")
    risk = case.get("risk_class")
    risk_error = None
    if risk in {"non_offer_submission", "condition_omission"}:
        risk_error = bool(snapshot["offers"])
    elif risk == "non_confirmation_agreement":
        risk_error = snapshot["session_status"] == "AGREED"
    return {"business_pass": not failures, "failures": failures, "risk_error": risk_error}
