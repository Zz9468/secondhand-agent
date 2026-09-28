from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from langsmith import Client

from app.core.config import Settings


class TraceSink(Protocol):
    def emit(self, payload: dict[str, object]) -> None:
        ...


class LangSmithTraceSink:
    """只镜像脱敏元数据；不启用 LangChain 自动提示词追踪。"""

    def __init__(self, settings: Settings) -> None:
        if not settings.langsmith_is_configured:
            raise ValueError("LangSmith 观测未完整配置")
        api_key = settings.observability_langsmith_api_key
        if api_key is None:  # pragma: no cover - 属性校验已保证
            raise ValueError("LangSmith API Key 缺失")
        self._project = settings.observability_langsmith_project
        self._client = Client(
            api_url=settings.observability_langsmith_endpoint,
            api_key=api_key.get_secret_value(),
            timeout_ms=int(settings.observability_langsmith_timeout_seconds * 1000),
            auto_batch_tracing=False,
            hide_inputs=True,
            hide_outputs=True,
            omit_traced_runtime_info=True,
        )

    def emit(self, payload: dict[str, object]) -> None:
        event_id = str(payload["event_id"])
        occurred_at = payload.get("created_at")
        timestamp = datetime.fromisoformat(str(occurred_at)) if occurred_at else datetime.now(UTC)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        else:
            timestamp = timestamp.astimezone(UTC)
        metadata = {
            key: value
            for key, value in payload.items()
            if key not in {"event_id", "created_at"}
        }
        self._client.create_run(
            name=f"secondhand.{payload['event_type']}",
            inputs={"redacted": True},
            outputs={"redacted": True, "outcome": payload["outcome"]},
            run_type="chain",
            project_name=self._project,
            id=UUID(event_id),
            start_time=timestamp,
            end_time=timestamp,
            extra={"metadata": metadata},
            tags=["secondhand-agent", str(payload["event_type"])],
        )


def build_trace_sink(settings: Settings) -> TraceSink | None:
    if not settings.langsmith_is_configured:
        return None
    return LangSmithTraceSink(settings)
