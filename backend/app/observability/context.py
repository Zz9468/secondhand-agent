from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ObservationContext:
    correlation_id: str
    http_request_id: str | None = None
    session_id: int | None = None
    model_task_id: int | None = None


_context: ContextVar[ObservationContext | None] = ContextVar(
    "secondhand_observation_context",
    default=None,
)


def new_correlation_id() -> str:
    return str(uuid4())


def current_observation_context() -> ObservationContext | None:
    return _context.get()


@contextmanager
def observation_scope(
    *,
    correlation_id: str | None = None,
    http_request_id: str | None = None,
    session_id: int | None = None,
    model_task_id: int | None = None,
) -> Iterator[ObservationContext]:
    """在同步 API 与 Worker 间传播最小关联信息，不保存业务正文。"""

    parent = current_observation_context()
    if parent is None:
        context = ObservationContext(
            correlation_id=correlation_id or new_correlation_id(),
            http_request_id=http_request_id,
            session_id=session_id,
            model_task_id=model_task_id,
        )
    else:
        context = replace(
            parent,
            correlation_id=correlation_id or parent.correlation_id,
            http_request_id=(
                http_request_id
                if http_request_id is not None
                else parent.http_request_id
            ),
            session_id=session_id if session_id is not None else parent.session_id,
            model_task_id=(
                model_task_id
                if model_task_id is not None
                else parent.model_task_id
            ),
        )
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)
