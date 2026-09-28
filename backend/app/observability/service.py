import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.models import ObservabilityEvent
from app.observability.context import current_observation_context, new_correlation_id
from app.observability.langsmith_sink import TraceSink, build_trace_sink
from app.services.model_task_service import ModelUsage

logger = logging.getLogger(__name__)

_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_SAFE_ATTRIBUTE_KEYS = {
    "agent_outcome",
    "approval_status",
    "confirmation_source",
    "formal_commitment",
    "idempotent_replay",
    "manual_action",
    "model_call_purpose",
    "retry_scheduled",
    "session_status",
    "task_status",
    "task_type",
    "tool_name",
}


@dataclass(frozen=True, slots=True)
class ObservabilityEventInput:
    event_type: str
    action: str
    outcome: str
    correlation_id: str | None = None
    http_request_id: str | None = None
    session_id: int | None = None
    model_task_id: int | None = None
    approval_id: int | None = None
    offer_id: int | None = None
    attempt_count: int | None = None
    duration_ms: int | None = None
    error_category: str | None = None
    usage: ModelUsage | None = None
    attributes: dict[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class ObservabilityEventSnapshot:
    id: int
    event_id: str
    event_version: int
    event_type: str
    action: str
    outcome: str
    correlation_id: str
    http_request_id: str | None
    session_id: int | None
    model_task_id: int | None
    approval_id: int | None
    offer_id: int | None
    attempt_count: int | None
    duration_ms: int | None
    error_category: str | None
    model_provider: str | None
    model_name: str | None
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    total_tokens: int | None
    input_price_per_million: Decimal | None
    output_price_per_million: Decimal | None
    cached_input_price_per_million: Decimal | None
    estimated_cost: Decimal | None
    cost_currency: str | None
    attributes: dict[str, object]
    created_at: datetime

    def to_json_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["estimated_cost"] = (
            str(self.estimated_cost) if self.estimated_cost is not None else None
        )
        for field_name in (
            "input_price_per_million",
            "output_price_per_million",
            "cached_input_price_per_million",
        ):
            value = payload[field_name]
            payload[field_name] = str(value) if value is not None else None
        payload["created_at"] = self.created_at.isoformat()
        return payload


class ObservabilityService:
    """写入脱敏本地事件，并以尽力而为方式镜像到外部 Trace。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        trace_sink: TraceSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._trace_sink = (
            trace_sink
            if trace_sink is not None
            else build_trace_sink(get_settings())
        )

    def record(
        self,
        event: ObservabilityEventInput,
    ) -> ObservabilityEventSnapshot | None:
        try:
            stored = self._validated_event(event)
            with self._session_factory() as db, db.begin():
                db.add(stored)
                db.flush()
                db.refresh(stored)
                snapshot = self._snapshot(stored)
        except (SQLAlchemyError, ValueError, TypeError):
            logger.warning(
                json.dumps(
                    {
                        "event_type": "OBSERVABILITY_WRITE_FAILED",
                        "action": event.action[:80],
                        "outcome": "ERROR",
                    },
                    ensure_ascii=False,
                )
            )
            return None

        payload = snapshot.to_json_dict()
        logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        if self._trace_sink is not None:
            try:
                self._trace_sink.emit(payload)
            except Exception:
                logger.warning(
                    json.dumps(
                        {
                            "event_type": "EXTERNAL_TRACE_FAILED",
                            "correlation_id": snapshot.correlation_id,
                            "outcome": "ERROR",
                        },
                        ensure_ascii=False,
                    )
                )
        return snapshot

    def list_events(
        self,
        *,
        session_id: int | None = None,
        correlation_id: str | None = None,
        model_task_id: int | None = None,
        error_category: str | None = None,
        min_attempt_count: int | None = None,
        limit: int = 1000,
    ) -> tuple[ObservabilityEventSnapshot, ...]:
        if not any((session_id, correlation_id, model_task_id)):
            raise ValueError("观测查询必须至少提供会话、关联或任务标识")
        if not 1 <= limit <= 10000:
            raise ValueError("观测查询数量必须在 1 到 10000 之间")
        statement = select(ObservabilityEvent).order_by(ObservabilityEvent.id).limit(limit)
        if session_id is not None:
            statement = statement.where(ObservabilityEvent.session_id == session_id)
        if correlation_id is not None:
            self._validated_uuid(correlation_id, field_name="关联标识")
            statement = statement.where(
                ObservabilityEvent.correlation_id == correlation_id
            )
        if model_task_id is not None:
            statement = statement.where(
                ObservabilityEvent.model_task_id == model_task_id
            )
        if error_category is not None:
            statement = statement.where(
                ObservabilityEvent.error_category == error_category
            )
        if min_attempt_count is not None:
            statement = statement.where(
                ObservabilityEvent.attempt_count >= min_attempt_count
            )
        with self._session_factory() as db:
            return tuple(self._snapshot(item) for item in db.scalars(statement))

    @classmethod
    def _validated_event(cls, event: ObservabilityEventInput) -> ObservabilityEvent:
        context = current_observation_context()
        correlation_id = event.correlation_id or (
            context.correlation_id if context is not None else new_correlation_id()
        )
        cls._validated_uuid(correlation_id, field_name="关联标识")
        event_type = cls._validated_name(event.event_type, field_name="事件类型", limit=50)
        action = cls._validated_name(event.action, field_name="动作", limit=80)
        outcome = cls._validated_name(event.outcome, field_name="结果", limit=32)
        attributes = cls._validated_attributes(event.attributes or {})
        usage = event.usage
        return ObservabilityEvent(
            event_id=str(uuid4()),
            event_version=1,
            event_type=event_type,
            action=action,
            outcome=outcome,
            correlation_id=correlation_id,
            http_request_id=(
                event.http_request_id
                or (context.http_request_id if context is not None else None)
            ),
            session_id=(
                event.session_id
                if event.session_id is not None
                else (context.session_id if context is not None else None)
            ),
            model_task_id=(
                event.model_task_id
                if event.model_task_id is not None
                else (context.model_task_id if context is not None else None)
            ),
            approval_id=event.approval_id,
            offer_id=event.offer_id,
            attempt_count=event.attempt_count,
            duration_ms=event.duration_ms,
            error_category=event.error_category,
            model_provider=usage.provider if usage else None,
            model_name=usage.model_name if usage else None,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            cached_input_tokens=usage.cached_input_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
            input_price_per_million=(
                usage.input_price_per_million if usage else None
            ),
            output_price_per_million=(
                usage.output_price_per_million if usage else None
            ),
            cached_input_price_per_million=(
                usage.cached_input_price_per_million if usage else None
            ),
            estimated_cost=usage.estimated_cost if usage else None,
            cost_currency=usage.cost_currency if usage else None,
            attributes=attributes,
        )

    @staticmethod
    def _validated_name(value: str, *, field_name: str, limit: int) -> str:
        if not isinstance(value, str) or len(value) > limit or not _NAME_PATTERN.fullmatch(value):
            raise ValueError(f"{field_name}必须是稳定的大写枚举标识")
        return value

    @staticmethod
    def _validated_uuid(value: str, *, field_name: str) -> str:
        try:
            parsed = UUID(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name}必须是 UUID") from exc
        if str(parsed) != value:
            raise ValueError(f"{field_name}必须使用标准 UUID 格式")
        return value

    @staticmethod
    def _validated_attributes(
        attributes: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        unknown = set(attributes) - _SAFE_ATTRIBUTE_KEYS
        if unknown:
            raise ValueError("观测属性包含未允许字段")
        result: dict[str, JsonValue] = {}
        for key, value in attributes.items():
            if isinstance(value, str):
                result[key] = value[:100]
            elif value is None or isinstance(value, (bool, int, float)):
                result[key] = value
            else:
                raise ValueError("观测属性只允许短字符串、数字、布尔值或空值")
        return result

    @staticmethod
    def _snapshot(event: ObservabilityEvent) -> ObservabilityEventSnapshot:
        return ObservabilityEventSnapshot(
            id=event.id,
            event_id=event.event_id,
            event_version=event.event_version,
            event_type=event.event_type,
            action=event.action,
            outcome=event.outcome,
            correlation_id=event.correlation_id,
            http_request_id=event.http_request_id,
            session_id=event.session_id,
            model_task_id=event.model_task_id,
            approval_id=event.approval_id,
            offer_id=event.offer_id,
            attempt_count=event.attempt_count,
            duration_ms=event.duration_ms,
            error_category=event.error_category,
            model_provider=event.model_provider,
            model_name=event.model_name,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            cached_input_tokens=event.cached_input_tokens,
            total_tokens=event.total_tokens,
            input_price_per_million=event.input_price_per_million,
            output_price_per_million=event.output_price_per_million,
            cached_input_price_per_million=event.cached_input_price_per_million,
            estimated_cost=event.estimated_cost,
            cost_currency=event.cost_currency,
            attributes=dict(event.attributes),
            created_at=event.created_at,
        )
