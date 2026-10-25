"""运行身份和运维审计与业务回执独立保留。"""

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.graph import TIME


class GraphExecutionInstance(Base):
    __tablename__ = "graph_execution_instances"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    input_id: Mapped[str] = mapped_column(ForeignKey("negotiation_inputs.id"), index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    ownership_epoch: Mapped[int | None]
    pg_pid: Mapped[int]
    pg_backend_start: Mapped[str] = mapped_column(String(64))
    application_name: Mapped[str] = mapped_column(String(64))
    registered_at: Mapped[datetime] = mapped_column(TIME)
    checked_at: Mapped[datetime | None] = mapped_column(TIME)
    state: Mapped[str] = mapped_column(String(24), index=True, default="REGISTERED")
    error_category: Mapped[str | None] = mapped_column(String(64))


class GraphServiceHeartbeat(Base):
    __tablename__ = "graph_service_heartbeats"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    service: Mapped[str] = mapped_column(String(24), index=True)
    last_seen: Mapped[datetime] = mapped_column(TIME)
    status: Mapped[str] = mapped_column(String(24))
