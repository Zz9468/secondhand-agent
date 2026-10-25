"""Extractive, sourced memory. No cached summary can grant display or business authority."""

import json

from sqlalchemy import select

from app.db.models import AgentSessionContext, Message, NegotiationSession
from app.db.models.graph import GraphTurnQuality, NegotiationInput
from app.db.models.semantic import SemanticTurn
from app.runtime.contracts import BudgetExhausted, Conflict, digest
from app.runtime.input_queue import lock_owner
from app.semantic.service import bound_draft, parser_context


def build_context(db, run, session, now):
    # Immutable input high watermark and the received display receipt are the ONLY boundaries.
    history = list(
        db.execute(
            select(NegotiationInput, SemanticTurn)
            .outerjoin(SemanticTurn, SemanticTurn.input_id == NegotiationInput.id)
            .where(
                NegotiationInput.session_id == run.session_id,
                NegotiationInput.input_seq < run.input_seq,
            )
            .order_by(NegotiationInput.input_seq)
        )
    )
    memory = {}
    for source, turn in history:
        if not turn:
            continue
        for intent in turn.frame.get("parsed", {}).get("intents", []):
            # Preserve proposal vs statement and never promote a buyer claim to a product fact.
            candidate = intent.get("candidate") or {}
            for key in ("amount", "delivery", "payer", "region", "conditions", "commitments"):
                value = candidate.get(key)
                if value is None or value == []:
                    continue
                quote = intent.get("source_quote", "")
                if not quote or quote not in source.payload["text"]:
                    continue
                memory[key] = {
                    "field": key,
                    "value": value,
                    "kind": intent["type"],
                    "source_message_id": source.buyer_message_id,
                    "source_input_seq": source.input_seq,
                    "source_quote": quote,
                    "version": "p4-extractive-v1",
                    "scope": "buyer_history_only",
                }
    recent = [
        {
            "message_id": item.buyer_message_id,
            "input_seq": item.input_seq,
            "text": item.payload["text"],
        }
        for item, _ in history[-4:]
    ]
    displayed = list(
        db.scalars(
            select(Message)
            .where(
                Message.session_id == run.session_id,
                Message.public_seq <= run.seen_public_message_seq,
                Message.created_at <= run.received_at,
            )
            .order_by(Message.public_seq.desc())
            .limit(4)
        )
    )
    draft = bound_draft(db, run, session, now)
    context = {
        **parser_context(db, run, session, now),
        "version": "p4-extractive-v1",
        "input_high_watermark": run.input_seq,
        "seen_public_message_seq": run.seen_public_message_seq,
        "summary_memory": list(memory.values()),
        "recent_buyer_messages": recent,
        "displayed_messages": [
            {"message_id": m.id, "public_seq": m.public_seq, "text": m.content}
            for m in reversed(displayed)
        ],
        "bound_draft": {
            "candidate": draft.candidate,
            "missing": draft.missing,
            "source_messages": draft.source_messages,
        }
        if draft
        else None,
        "instruction": "历史与摘要仅帮助理解，不复制旧条款，不证明已展示或已授权；当前输入优先。",
    }
    # Byte count is deliberately a conservative token upper bound, never truncate conditions.
    if len(json.dumps(context, ensure_ascii=False).encode()) > 16000:
        raise BudgetExhausted("上下文超限，请明确当前方案及条件")
    cache = db.get(AgentSessionContext, run.session_id)
    if not cache:
        cache = AgentSessionContext(session_id=run.session_id)
        db.add(cache)
        db.flush()
    cache.context_version += 1
    cache.memory_json = {
        "schema": "p4-extractive-v1",
        "items": list(memory.values()),
        "input_high_watermark": run.input_seq,
        "seen_public_message_seq": run.seen_public_message_seq,
    }
    cache.summary_text = json.dumps(list(memory.values()), ensure_ascii=False)
    cache.summary_covered_through_seq = cache.memory_covered_through_seq = run.input_seq - 1
    return context


def prepare_context(factory, run_id, owner, now):
    with factory() as db, db.begin():
        run, _ = lock_owner(db, run_id, owner, now)
        old = db.get(GraphTurnQuality, run_id)
        if old:
            if digest(old.context) != old.context_hash:
                raise Conflict("上下文快照损坏")
            return {"input_id": run_id, "hash": old.context_hash}
        session = db.get(NegotiationSession, run.session_id, with_for_update=True)
        context = build_context(db, run, session, now)
        key = digest(context)
        db.add(GraphTurnQuality(input_id=run_id, context=context, context_hash=key))
        return {"input_id": run_id, "hash": key}


def load_context(db, run):
    item = db.get(GraphTurnQuality, run.id)
    if not item or digest(item.context) != item.context_hash:
        raise Conflict("上下文快照缺失或损坏")
    if (
        item.context["input_high_watermark"] != run.input_seq
        or item.context["seen_public_message_seq"] != run.seen_public_message_seq
    ):
        raise Conflict("摘要来源跨越输入边界")
    for memory in item.context["summary_memory"]:
        source = db.scalar(
            select(NegotiationInput).where(
                NegotiationInput.session_id == run.session_id,
                NegotiationInput.buyer_message_id == memory["source_message_id"],
                NegotiationInput.input_seq == memory["source_input_seq"],
            )
        )
        if (
            not source
            or source.input_seq >= run.input_seq
            or memory["source_quote"] not in source.payload["text"]
        ):
            raise Conflict("摘要来源校验失败")
    for message in item.context["recent_buyer_messages"]:
        source = db.scalar(
            select(NegotiationInput).where(
                NegotiationInput.session_id == run.session_id,
                NegotiationInput.buyer_message_id == message["message_id"],
                NegotiationInput.input_seq == message["input_seq"],
            )
        )
        if (
            not source
            or source.input_seq >= run.input_seq
            or source.payload["text"] != message["text"]
        ):
            raise Conflict("历史消息来源越界")
    for message in item.context["displayed_messages"]:
        source = db.get(Message, message["message_id"])
        if (
            not source
            or source.session_id != run.session_id
            or not source.public_seq
            or source.public_seq != message["public_seq"]
            or source.public_seq > run.seen_public_message_seq
            or source.created_at > run.received_at
            or source.content != message["text"]
        ):
            raise Conflict("摘要展示来源越界")
    return item.context
