from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AgentMessage,
    AgentSessionContext,
    Message,
    MessageRole,
    NegotiationSession,
)


class AgentMessageService:
    """All callers own a short transaction; no network calls occur here."""

    @staticmethod
    def context(db: Session, session_id: int) -> AgentSessionContext:
        # A common parent lock also serializes first-time context row creation.
        db.get(NegotiationSession, session_id, with_for_update=True)
        row = db.get(AgentSessionContext, session_id, with_for_update=True)
        if row is None:
            row = AgentSessionContext(session_id=session_id)
            db.add(row)
            db.flush()
        return row

    @classmethod
    def append(
        cls,
        db: Session,
        *,
        session_id: int,
        event_key: str,
        role: str,
        kind: str,
        payload: dict,
        visibility: str = "INTERNAL",
        task_id: int | None = None,
        branch_id: str | None = None,
        step_id: str | None = None,
        lease_attempt: int | None = None,
        tool_call_id: str | None = None,
    ) -> AgentMessage:
        context = cls.context(db, session_id)
        existing = db.scalar(
            select(AgentMessage).where(
                AgentMessage.session_id == session_id, AgentMessage.event_key == event_key
            )
        )
        if existing is not None:
            if existing.payload != payload or existing.kind != kind:
                raise ValueError("Agent event key conflicts with its immutable payload")
            return existing
        row = AgentMessage(
            session_id=session_id,
            stream_seq=context.next_stream_seq,
            model_task_id=task_id,
            run_branch_id=branch_id,
            logical_step_id=step_id,
            lease_attempt=lease_attempt,
            role=role,
            kind=kind,
            visibility=visibility,
            event_key=event_key,
            tool_call_id=tool_call_id,
            payload=deepcopy(payload),
        )
        context.next_stream_seq += 1
        db.add(row)
        db.flush()
        return row

    @classmethod
    def project(
        cls,
        db: Session,
        message: Message,
        *,
        task_id: int | None = None,
        kind: str | None = None,
        legacy: bool = False,
    ) -> AgentMessage:
        if message.source_agent_message_id is not None:
            return db.get(AgentMessage, message.source_agent_message_id)
        buyer = message.role is MessageRole.BUYER
        row = cls.append(
            db,
            session_id=message.session_id,
            event_key=f"public:{message.request_id}",
            role="USER" if buyer else "ASSISTANT",
            kind=kind or ("BUYER_INPUT" if buyer else "PUBLIC_REPLY"),
            visibility="PUBLIC",
            task_id=task_id,
            payload={
                "content": message.content,
                "request_id": message.request_id,
                "public_role": message.role.value,
                "formal_offer_id": message.formal_offer_id,
                "outcome": message.agent_outcome,
                "legacy": legacy,
            },
        )
        message.source_agent_message_id = row.id
        db.flush()
        return row

    @classmethod
    def backfill(cls, db: Session, session_id: int) -> int:
        context = cls.context(db, session_id)
        rows = list(
            db.scalars(
                select(Message)
                .where(Message.session_id == session_id, Message.source_agent_message_id.is_(None))
                .order_by(Message.id)
            )
        )
        for message in rows:
            cls.project(
                db,
                message,
                legacy=True,
                kind="APPROVAL_NOTIFICATION" if message.role is MessageRole.SYSTEM else None,
            )
        context.legacy_backfill_status = "COMPLETE"
        db.flush()
        return len(rows)

    @staticmethod
    def records(
        db: Session, session_id: int, *, after_seq: int = 0, through_seq: int | None = None
    ) -> list[AgentMessage]:
        query = select(AgentMessage).where(
            AgentMessage.session_id == session_id, AgentMessage.stream_seq > after_seq
        )
        if through_seq is not None:
            query = query.where(AgentMessage.stream_seq <= through_seq)
        return list(db.scalars(query.order_by(AgentMessage.stream_seq)))
