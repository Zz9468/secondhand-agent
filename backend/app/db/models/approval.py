from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.enums import (
    ApprovalFollowupStatus,
    ApprovalStatus,
    stored_enum,
)

if TYPE_CHECKING:
    from app.db.models.negotiation import NegotiationSession
    from app.db.models.offer import Offer


class ApprovalRequest(TimestampMixin, Base):
    """针对单个不可变报价创建的卖家审批事实。"""

    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint("policy_version > 0", name="policy_version_positive"),
        UniqueConstraint("offer_id", name="uq_approval_requests_offer"),
        UniqueConstraint(
            "pending_session_id",
            name="uq_approval_requests_pending_session",
        ),
        UniqueConstraint(
            "followup_request_id",
            name="uq_approval_requests_followup_request",
        ),
        Index("ix_approval_requests_session_status", "session_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("negotiation_sessions.id"),
        nullable=False,
    )
    offer_id: Mapped[int] = mapped_column(
        ForeignKey("offers.id", ondelete="CASCADE"),
        nullable=False,
    )
    policy_version: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        stored_enum(ApprovalStatus, name="approval_status"),
        nullable=False,
        default=ApprovalStatus.PENDING,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    seller_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    followup_status: Mapped[ApprovalFollowupStatus | None] = mapped_column(
        stored_enum(ApprovalFollowupStatus, name="approval_followup_status"),
        nullable=True,
    )
    followup_request_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    # MySQL 唯一索引允许多个 NULL，以此约束每个会话最多一个待审批记录。
    pending_session_id: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed(
            "CASE WHEN status = 'PENDING' THEN session_id ELSE NULL END",
            persisted=True,
        ),
        nullable=True,
    )

    session: Mapped["NegotiationSession"] = relationship(
        back_populates="approval_requests"
    )
    offer: Mapped["Offer"] = relationship(back_populates="approval_requests")
