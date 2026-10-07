"""Idempotent public-history backfill and projection consistency verification."""

import argparse
import json

from sqlalchemy import select

from app.db.models import AgentMessage, Message, NegotiationSession
from app.db.session import get_session_factory
from app.services.agent_message_service import AgentMessageService


def backfill(factory, *, verify_only=False):
    with factory() as db:
        session_ids = list(
            db.scalars(select(NegotiationSession.id).order_by(NegotiationSession.id))
        )
    imported = 0
    mismatches = []
    for session_id in session_ids:
        with factory() as db, db.begin():
            if not verify_only:
                imported += AgentMessageService.backfill(db, session_id)
            for message in db.scalars(select(Message).where(Message.session_id == session_id)):
                source = (
                    db.get(AgentMessage, message.source_agent_message_id)
                    if message.source_agent_message_id
                    else None
                )
                if (
                    source is None
                    or source.session_id != session_id
                    or source.visibility != "PUBLIC"
                    or source.payload["content"] != message.content
                    or source.payload.get("public_role") != message.role.value
                    or source.payload.get("request_id") != message.request_id
                    or source.payload.get("formal_offer_id") != message.formal_offer_id
                    or source.payload.get("outcome") != message.agent_outcome
                ):
                    mismatches.append(message.id)
    return {
        "sessions": len(session_ids),
        "imported": imported,
        "projection_mismatches": mismatches,
        "passed": not mismatches,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    report = backfill(get_session_factory(), verify_only=args.verify_only)
    print(json.dumps(report))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
