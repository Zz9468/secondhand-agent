"""Local causal evidence. No private prices or bearer display credentials are exported."""

from app.db.models import Offer
from app.db.models.graph import BusinessActionReceipt, BusinessCausalEvent, GraphOfferEvidence
from app.db.models.semantic import CostQuote, SemanticOfferBasis
from app.runtime.contracts import digest

STAGES = ("evidence_resolved", "authorization_checked", "action_committed", "result_published")


def append(db, action_id, sequence, evidence):
    event = BusinessCausalEvent(
        id=f"{action_id}:{sequence}",
        action_id=action_id,
        sequence=sequence,
        stage=STAGES[sequence - 1],
        parent_id=f"{action_id}:{sequence - 1}" if sequence > 1 else None,
        evidence=evidence,
    )
    db.add(event)
    db.flush()
    return event


def input_basis(db, run, session, product, policy, payload, candidate, quote, evaluations):
    from app.services.graph_business_operations import terms_hash

    offer = db.get(Offer, payload.offer_id) if payload.offer_id else None
    basis = db.get(SemanticOfferBasis, offer.id) if offer else None
    proof = db.get(GraphOfferEvidence, offer.id) if offer else None
    if basis and candidate is None:
        conditions = basis.candidate.get("conditions", [])
        commitments = basis.candidate.get("commitments", [])
        quote = db.get(CostQuote, basis.cost_quote_id)
        evaluations = basis.evaluations
    else:
        conditions = [v.model_dump(mode="json") for v in candidate.conditions] if candidate else []
        commitments = candidate.commitments if candidate else []
    return {
        "schema": 1,
        "logical_action_id": f"turn:{run.id}:action",
        "session_id": session.id,
        "input_id": run.id,
        "input_seq": run.input_seq,
        "command": payload.kind,
        # Stable before insertion; commit maps this proposal to the actual offer.
        "proposal_id": f"input:{run.id}",
        "object_offer_id": offer.id if offer else None,
        "terms_hash": digest(payload.terms.model_dump(mode="json"))
        if payload.terms
        else terms_hash(offer)
        if offer
        else digest(None),
        "conditions_hash": digest([conditions, commitments]),
        "evidence_hash": digest(
            [
                quote.snapshot if quote else None,
                evaluations,
                proof.product_hash if proof else None,
            ]
        ),
        "evidence_versions": {
            "product": product.product_version,
            "policy": policy.version,
            "cost_quote": quote.id if quote else None,
        },
        "before_version": session.version,
        "display_evidence_hash": digest(
            [
                run.payload.get("display_receipt_id"),
                run.seen_public_message_seq,
            ]
        )
        if payload.kind == "CONFIRM"
        else None,
    }


def committed(db, action_id, evidence, result, after_version):
    return append(
        db,
        action_id,
        3,
        {
            **evidence,
            "offer_id": result.get("offer_id"),
            "after_version": after_version,
            "result_hash": digest(result),
        },
    )


def published(db, run, message):
    if not run:
        return
    action_id = f"turn:{run.id}:action"
    receipt = db.get(BusinessActionReceipt, action_id)
    previous = db.get(BusinessCausalEvent, f"{action_id}:3")
    # Historical pre-P5 receipts remain explicitly without causal evidence.
    if receipt and previous:
        append(db, action_id, 4, {**previous.evidence, "message_id": message.id})
