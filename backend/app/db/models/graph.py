"""新图的业务协议表；执行位置仅保存在官方 Checkpointer。"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

TIME = DateTime().with_variant(DATETIME(fsp=6), "mysql")


class NegotiationInput(Base):
    __tablename__ = "negotiation_inputs"
    __table_args__ = (
        UniqueConstraint("session_id", "input_seq", name="uq_negotiation_inputs_sequence"),
        UniqueConstraint("session_id", "request_id", name="uq_negotiation_inputs_request"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("negotiation_sessions.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("model_execution_tasks.id"), unique=True)
    buyer_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), unique=True)
    input_seq: Mapped[int]
    request_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    input_hash: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON)
    config_hash: Mapped[str] = mapped_column(String(64))
    graph_version: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[datetime] = mapped_column(TIME)
    seen_public_message_seq: Mapped[int] = mapped_column(default=0)
    checkpoint_ref: Mapped[str | None] = mapped_column(String(100))
    recovery_required: Mapped[bool] = mapped_column(default=False)


class BusinessActionReceipt(Base):
    __tablename__ = "business_action_receipts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    input_id: Mapped[str] = mapped_column(ForeignKey("negotiation_inputs.id"), unique=True)
    command_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    before_version: Mapped[int]
    after_version: Mapped[int]
    committed_at: Mapped[datetime] = mapped_column(TIME)


class GraphOfferEvidence(Base):
    __tablename__ = "graph_offer_evidence"
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), primary_key=True)
    terms_hash: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[int]
    product_hash: Mapped[str] = mapped_column(String(64))


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    input_id: Mapped[str] = mapped_column(ForeignKey("negotiation_inputs.id"), unique=True)
    source_thread_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(24), default="PENDING", index=True)
    result: Mapped[dict] = mapped_column(JSON)
    reply_deadline: Mapped[datetime] = mapped_column(TIME)
    public_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), unique=True)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(TIME)
    attempts: Mapped[int] = mapped_column(default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(TIME)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(ForeignKey("notification_deliveries.id"), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", index=True)


class PublicInteractionContext(Base):
    __tablename__ = "public_interaction_contexts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("negotiation_sessions.id"), index=True)
    buyer_id: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)
    public_seq: Mapped[int]
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("offers.id"))
    draft_refs: Mapped[list | None] = mapped_column(JSON)
    refresh_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    terms_hash: Mapped[str] = mapped_column(String(64))
    business_version: Mapped[int]
    expires_at: Mapped[datetime] = mapped_column(TIME)


class DisplayAck(Base):
    __tablename__ = "display_acks"
    __table_args__ = (UniqueConstraint("buyer_id", "client_view_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    context_id: Mapped[str] = mapped_column(
        ForeignKey("public_interaction_contexts.id"), index=True
    )
    buyer_id: Mapped[str] = mapped_column(String(64))
    client_view_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64))
    displayed_at: Mapped[datetime] = mapped_column(TIME)


class ModelCallLedger(Base):
    __tablename__ = "model_call_ledger"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    input_id: Mapped[str] = mapped_column(ForeignKey("negotiation_inputs.id"), index=True)
    step_key: Mapped[str] = mapped_column(String(64), index=True)
    accepted_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    ownership_epoch: Mapped[int]
    status: Mapped[str] = mapped_column(String(24))
    reserved_tokens: Mapped[int] = mapped_column(BigInteger)
    actual_tokens: Mapped[int | None] = mapped_column(BigInteger)
    reserved_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    value: Mapped[dict | None] = mapped_column(JSON)
    value_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TIME)
