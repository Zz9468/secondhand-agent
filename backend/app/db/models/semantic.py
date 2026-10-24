"""P2 audit records, trusted seller facts and immutable quotation evidence."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.graph import TIME


class ProductFactValue(Base):
    __tablename__ = "product_fact_values"
    __table_args__ = (UniqueConstraint("product_id", "attribute", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    attribute: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int]
    source: Mapped[str] = mapped_column(String(32), default="SELLER_CONFIRMED")
    confirmed_by: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"))
    confirmed_at: Mapped[datetime] = mapped_column(TIME)


class ShippingCostRule(Base):
    __tablename__ = "shipping_cost_rules"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    region: Mapped[str] = mapped_column(String(80))
    delivery: Mapped[str] = mapped_column(String(16), default="shipping")
    shipping_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    version: Mapped[int]
    active: Mapped[bool] = mapped_column(default=True)
    expires_at: Mapped[datetime] = mapped_column(TIME)
    confirmed_at: Mapped[datetime] = mapped_column(TIME)
    confirmed_by: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"))


class CostQuote(Base):
    __tablename__ = "cost_quotes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    scope: Mapped[dict] = mapped_column(JSON)
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TIME)
    expires_at: Mapped[datetime] = mapped_column(TIME)


class OfferDraft(Base):
    __tablename__ = "offer_drafts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("negotiation_sessions.id"), index=True)
    input_id: Mapped[str] = mapped_column(ForeignKey("negotiation_inputs.id"), index=True)
    version: Mapped[int] = mapped_column(default=1)
    candidate: Mapped[dict] = mapped_column(JSON)
    source_messages: Mapped[list] = mapped_column(JSON)
    alternative_group: Mapped[str | None] = mapped_column(String(64))
    missing: Mapped[list] = mapped_column(JSON)
    business_version: Mapped[int]
    related_offer_id: Mapped[int | None] = mapped_column(ForeignKey("offers.id"))
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    expires_at: Mapped[datetime] = mapped_column(TIME)


class VerificationItem(Base):
    __tablename__ = "draft_verifications"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey("offer_drafts.id"), unique=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="OPEN")
    reasons: Mapped[list] = mapped_column(JSON)
    notified_version: Mapped[int | None]


class SemanticTurn(Base):
    __tablename__ = "semantic_turns"
    input_id: Mapped[str] = mapped_column(ForeignKey("negotiation_inputs.id"), primary_key=True)
    frame: Mapped[dict] = mapped_column(JSON)
    command: Mapped[dict] = mapped_column(JSON)
    results: Mapped[list] = mapped_column(JSON)
    draft_ids: Mapped[list] = mapped_column(JSON)
    selected_draft_id: Mapped[str | None] = mapped_column(ForeignKey("offer_drafts.id"))


class SemanticOfferBasis(Base):
    __tablename__ = "semantic_offer_bases"
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), primary_key=True)
    cost_quote_id: Mapped[str] = mapped_column(ForeignKey("cost_quotes.id"))
    candidate: Mapped[dict] = mapped_column(JSON)
    evaluations: Mapped[list] = mapped_column(JSON)
