import json
import logging
import time
from uuid import UUID

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.observability.context import new_correlation_id, observation_scope

logger = logging.getLogger(__name__)


class RequestObservationMiddleware(BaseHTTPMiddleware):
    """为 HTTP 请求建立关联上下文，只记录不含正文和身份信息的元数据。"""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = self._safe_id(request.headers.get("X-Request-ID"))
        correlation_id = self._safe_id(request.headers.get("X-Correlation-ID"))
        started = time.perf_counter()
        status_code = 500
        with observation_scope(
            correlation_id=correlation_id,
            http_request_id=request_id,
        ) as context:
            try:
                response = await call_next(request)
                status_code = response.status_code
            finally:
                logger.info(
                    json.dumps(
                        {
                            "event_type": "HTTP_REQUEST_COMPLETED",
                            "action": f"{request.method} {request.url.path}",
                            "outcome": "SUCCESS" if status_code < 500 else "ERROR",
                            "correlation_id": context.correlation_id,
                            "http_request_id": context.http_request_id,
                            "status_code": status_code,
                            "duration_ms": round(
                                (time.perf_counter() - started) * 1000
                            ),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            response.headers["X-Request-ID"] = context.http_request_id or request_id
            response.headers["X-Correlation-ID"] = context.correlation_id
            return response

    @staticmethod
    def _safe_id(value: str | None) -> str:
        if value:
            try:
                parsed = UUID(value)
                if str(parsed) == value:
                    return value
            except ValueError:
                pass
        return new_correlation_id()
