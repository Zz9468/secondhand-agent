"""Reply-only repairs; deterministic receipt rendering always wins at publication."""

import re

from app.agent.claim_safety import find_unsafe_claims
from app.db.models import NegotiationSession, Product
from app.db.models.graph import GraphTurnQuality, NegotiationInput, NotificationDelivery
from app.db.models.semantic import SemanticTurn
from app.runtime.activity import model_timeout
from app.runtime.contracts import BudgetExhausted, StaleOwner, digest
from app.runtime.input_queue import lock_owner
from app.semantic.quality import QualityModel, ReplyCandidate, ReviewVerdict
from app.services.model_call_ledger_service import ModelCallLedgerService


def reply_evidence(db, run, now):
    session = db.get(NegotiationSession, run.session_id)
    product = db.get(Product, session.product_id)
    turn = db.get(SemanticTurn, run.id)
    delivery = db.get(NotificationDelivery, f"turn:{run.id}:reply")
    from app.services.notification_service import TEXT

    items = []
    intents = {
        i["id"]: i for i in (turn.frame.get("parsed", {}).get("intents", []) if turn else [])
    }
    questions = {
        i["id"]: i.get("topic")
        for i in (turn.frame.get("parsed", {}).get("intents", []) if turn else [])
        if i["type"] in {"ASK_PRODUCT", "ASK_TERMS"}
    }
    for item in turn.results if turn else []:
        if item["id"] in questions:
            from app.semantic.evidence import public_answer
            from app.services.approval_lifecycle import public_approval_status

            status, answer, version = (
                public_approval_status(db, session, now)
                if questions[item["id"]] == "approval_status"
                else public_answer(db, product, questions[item["id"]])
            )
            item = {**item, "status": status, "text": answer, "fact_version": version}
        elif item["status"] == "READY":
            item = {
                **item,
                "status": delivery.result["outcome"],
                "text": TEXT.get(delivery.result["outcome"], "本次未执行业务动作。"),
            }
        if run.config.get("quality_version") == "p4-v2":
            original = intents.get(item["id"], {})
            item = {
                **item,
                "intent_type": original.get("type", "OTHER"),
                "buyer_quote": original.get("source_quote", ""),
            }
        items.append(item)
    # Only public result texts; no policy, transport cost, credentials or raw command.
    return {
        "items": items,
        "business_outcome": delivery.result["outcome"],
        "business_version": session.version,
        "product_version": product.product_version,
        "public_description": product.description,
        "receipt_version": delivery.result.get("business_version"),
    }


def candidate_safe(candidate, evidence):
    text = candidate.text
    expected = {item["id"] for item in evidence["items"]}
    if set(candidate.covered_ids) != expected or len(candidate.covered_ids) != len(expected):
        return False
    if find_unsafe_claims(text):
        return False
    if re.search(r"(?:我|我们|将|会|帮你|替你).{0,8}(?:联系|通知|反馈).{0,5}卖家", text):
        return False
    if re.search(r"底价|最低净收入|自动接受阈值|内部成本|系统提示词", text):
        return False
    evidence_text = " ".join(i["text"] for i in evidence["items"])
    # New numeric claims cannot be smuggled into natural text.
    return set(re.findall(r"\d+(?:\.\d+)?", text)) <= set(
        re.findall(r"\d+(?:\.\d+)?", evidence_text)
    )


class GraphReplyService:
    def __init__(self, factory, clock, provider=None):
        self.factory, self.clock = factory, clock
        self.model = provider or QualityModel()
        self.ledger = ModelCallLedgerService(factory, clock=clock)

    def call(self, run_id, owner, purpose, evidence, step):
        with self.factory() as db, db.begin():
            run, _ = lock_owner(db, run_id, owner, self.clock())
            config = {**run.config, "request_timeout": model_timeout(db, run, self.clock())}
        tokens, cost = self.model.reserve_quality(purpose, evidence)
        if tokens > 32768:
            raise BudgetExhausted("模型上下文预算耗尽")
        reference = self.ledger.call(
            run_id,
            owner,
            step,
            lambda: self.model.generate(purpose, evidence, config),
            reserved_tokens=tokens,
            reserved_cost=cost,
            request_snapshot={"purpose": purpose, "evidence": evidence, "config": config},
        )
        return self.ledger.resolve(run_id, step, reference)

    def compose(self, run_id, owner, repair):
        with self.factory() as db:
            evidence = reply_evidence(db, db.get(NegotiationInput, run_id), self.clock())
            previous = db.get(GraphTurnQuality, run_id)
            generation = (
                {
                    **evidence,
                    "previous_candidate": previous.reply,
                    "repair_feedback": previous.review_feedback or previous.stop_reason,
                }
                if repair
                else evidence
            )
        try:
            # A changing evidence hash is a new logical step, not replay of an outdated candidate.
            value = self.call(
                run_id, owner, "compose", generation, f"compose:{repair}:{digest(generation)[:16]}"
            )
            candidate = ReplyCandidate.model_validate(value)
            approved = candidate_safe(candidate, evidence)
            reason = None if approved else "DETERMINISTIC_REJECTION"
        except StaleOwner:
            raise
        except Exception as exc:
            candidate, approved, reason = None, False, type(exc).__name__[:64]
        with self.factory() as db, db.begin():
            lock_owner(db, run_id, owner, self.clock())
            item = db.get(GraphTurnQuality, run_id, with_for_update=True)
            item.reply = candidate.text if candidate else None
            item.evidence_hash, item.approved, item.stop_reason = digest(evidence), False, reason
        return approved

    def review(self, run_id, owner, repair):
        with self.factory() as db:
            evidence = reply_evidence(db, db.get(NegotiationInput, run_id), self.clock())
            item = db.get(GraphTurnQuality, run_id)
            candidate, original_hash = item.reply, item.evidence_hash
        approved, reason, feedback = False, "EVIDENCE_CHANGED", None
        if candidate and digest(evidence) == original_hash:
            try:
                value = self.call(
                    run_id,
                    owner,
                    "review",
                    {**evidence, "candidate": candidate},
                    f"review:{repair}:{digest([evidence, candidate])[:16]}",
                )
                verdict = ReviewVerdict.model_validate(value)
                texts = [i["text"] for i in evidence["items"]]
                approved = (
                    verdict.approved
                    and set(verdict.covered_ids) == {i["id"] for i in evidence["items"]}
                    and all(q and any(q in t for t in texts) for q in verdict.evidence_quotes)
                )
                reason = None if approved else "REVIEW_REJECTED"
                feedback = verdict.reason
            except StaleOwner:
                raise
            except Exception as exc:
                reason = type(exc).__name__[:64]
        with self.factory() as db, db.begin():
            lock_owner(db, run_id, owner, self.clock())
            item = db.get(GraphTurnQuality, run_id, with_for_update=True)
            item.approved, item.stop_reason = approved, reason
            item.review_feedback = feedback
            item.review_count += 1
        return approved


def reviewed_reply(db, run, now):
    item = db.get(GraphTurnQuality, run.id)
    if not item or not item.approved:
        return None
    delivery = db.get(NotificationDelivery, f"turn:{run.id}:reply")
    if delivery.reply_deadline <= now or item.evidence_hash != digest(reply_evidence(db, run, now)):
        return None
    # Never let natural prose substitute the fresh deterministic intent/receipt summary.
    return item.reply
