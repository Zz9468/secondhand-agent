"""Public semantic summaries, draft cards and refreshable object-bound contexts."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.db.models import Offer, Product, SellerPolicy
from app.db.models.graph import PublicInteractionContext
from app.db.models.semantic import OfferDraft, SemanticTurn
from app.runtime.contracts import Conflict, digest
from app.semantic.service import usable_draft
from app.services.graph_business_operations import confirmation_expiry, terms_hash, valid_offer


def decorate(db, run, session, result, content, card, now):
    turn = db.get(SemanticTurn, run.id)
    if not turn:
        return content, card, None
    items = []
    progress_ids = {
        i["id"]
        for i in turn.frame.get("parsed", {}).get("intents", [])
        if i.get("topic") == "approval_status"
    }
    questions = {
        i["id"]: i.get("topic")
        for i in turn.frame.get("parsed", {}).get("intents", [])
        if i["type"] in {"ASK_PRODUCT", "ASK_TERMS"}
    }
    for item in turn.results:
        if item["id"] in progress_ids:
            from app.services.approval_lifecycle import public_approval_status

            status, text, _ = public_approval_status(db, session, now)
            items.append({**item, "status": status, "text": text})
        elif run.graph_version == "negotiation-v3" and item["id"] in questions:
            from app.semantic.evidence import public_answer

            status, answer, version = public_answer(
                db, db.get(Product, session.product_id), questions[item["id"]]
            )
            items.append({**item, "status": status, "text": answer, "fact_version": version})
        elif item["status"] == "READY":
            items.append({**item, "status": result["outcome"], "text": content})
        else:
            items.append(item)
    if result["outcome"] == "NOT_EXECUTED" and not any(
        i["status"] == "READY" for i in turn.results
    ):
        items.append({"id": "business", "status": "NOT_EXECUTED", "text": content})
    content = "\n".join(f"{index + 1}. {item['text']}" for index, item in enumerate(items))
    draft_refs, options = [], []
    for key in turn.draft_ids:
        draft = db.get(OfferDraft, key)
        if usable_draft(draft, session, now, draft.version):
            draft_refs.append({"id": draft.id, "version": draft.version})
            options.append(
                {
                    "draft_id": draft.id,
                    "draft_version": draft.version,
                    "candidate": draft.candidate,
                    "missing": draft.missing,
                }
            )
    if draft_refs and not card:
        card = {
            "kind": "drafts",
            "context_id": uuid4().hex,
            "drafts": options,
            "can_confirm": False,
            "expires_at": min(
                db.get(OfferDraft, r["id"]).expires_at for r in draft_refs
            ).isoformat()
            + "Z",
        }
    if card:
        card = {"kind": "offer", **card, "intent_results": items}
    return content, card, draft_refs or None


def check_object(db, context, session, now):
    if context.business_version != session.version:
        raise Conflict("展示对象业务版本已失效")
    if context.offer_id:
        product = db.get(Product, session.product_id, with_for_update=True)
        policy = db.scalar(select(SellerPolicy).where(SellerPolicy.product_id == product.id))
        offer = db.get(Offer, context.offer_id)
        if (
            not valid_offer(db, session, product, policy, offer, now)
            or terms_hash(offer) != context.terms_hash
        ):
            raise Conflict("报价或授权依据已失效，请重新协商")
        return confirmation_expiry(db, offer, now)
    refs = context.draft_refs or []
    drafts = [db.get(OfferDraft, ref["id"]) for ref in refs]
    if not refs or any(
        not usable_draft(d, session, now, r["version"]) for d, r in zip(drafts, refs, strict=True)
    ):
        raise Conflict("澄清草案已失效，请重新协商")
    return min(d.expires_at for d in drafts)


def refresh_context(factory, sid, buyer, context_id, request_id, now):
    from app.runtime.input_queue import owned_session

    with factory() as db, db.begin():
        session = owned_session(db, sid, buyer, lock=True)
        original = db.get(PublicInteractionContext, context_id)
        if not original or original.session_id != sid or original.buyer_id != buyer:
            raise Conflict("展示上下文不存在或无权访问")
        expiry = check_object(db, original, session, now)
        key = digest([sid, buyer, request_id])
        old = db.scalar(
            select(PublicInteractionContext).where(PublicInteractionContext.refresh_key == key)
        )
        if old:
            if (
                old.message_id != original.message_id
                or old.offer_id != original.offer_id
                or old.draft_refs != original.draft_refs
            ):
                raise Conflict("刷新请求载荷冲突")
            return {"context_id": old.id, "expires_at": old.expires_at.isoformat() + "Z"}
        context = PublicInteractionContext(
            id=uuid4().hex,
            session_id=sid,
            buyer_id=buyer,
            message_id=original.message_id,
            public_seq=original.public_seq,
            offer_id=original.offer_id,
            terms_hash=original.terms_hash,
            draft_refs=original.draft_refs,
            business_version=original.business_version,
            expires_at=min(expiry, now + timedelta(minutes=30)),
            refresh_key=key,
        )
        db.add(context)
        return {"context_id": context.id, "expires_at": context.expires_at.isoformat() + "Z"}
