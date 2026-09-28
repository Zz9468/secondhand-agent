import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from langchain.agents.structured_output import StructuredOutputValidationError
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from pydantic import ValidationError

from app.core.config import Settings
from app.db.models import ModelTaskErrorCategory


@dataclass(frozen=True, slots=True)
class ModelFailureClassification:
    category: ModelTaskErrorCategory
    retryable: bool
    safe_message: str


@dataclass(frozen=True, slots=True)
class ModelRetryPlan:
    category: ModelTaskErrorCategory
    should_retry: bool
    next_retry_at: datetime | None
    safe_message: str


class ModelRetryPolicy:
    """统一模型错误分类、指数退避和最大尝试次数。"""

    def __init__(
        self,
        *,
        max_attempts: int,
        base_delay_seconds: float,
        max_delay_seconds: float,
        jitter_ratio: float,
        lease_seconds: int,
    ) -> None:
        if not 1 <= max_attempts <= 20:
            raise ValueError("模型任务最大尝试次数必须在 1 到 20 之间")
        if base_delay_seconds <= 0 or max_delay_seconds <= 0:
            raise ValueError("模型任务退避时间必须大于零")
        if not 0 <= jitter_ratio <= 0.5:
            raise ValueError("模型任务抖动比例必须在 0 到 0.5 之间")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("模型任务租约时长必须在 1 到 3600 秒之间")
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.jitter_ratio = jitter_ratio
        self.lease_seconds = lease_seconds

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelRetryPolicy":
        return cls(
            max_attempts=settings.model_task_max_attempts,
            base_delay_seconds=settings.model_task_retry_base_seconds,
            max_delay_seconds=settings.model_task_retry_max_seconds,
            jitter_ratio=settings.model_task_retry_jitter_ratio,
            lease_seconds=settings.model_task_lease_seconds,
        )

    def plan_failure(
        self,
        *,
        task_id: int,
        attempt_count: int,
        max_attempts: int,
        error: BaseException,
        now: datetime | None = None,
    ) -> ModelRetryPlan:
        classification = self.classify(error)
        if not classification.retryable or attempt_count >= max_attempts:
            return ModelRetryPlan(
                category=classification.category,
                should_retry=False,
                next_retry_at=None,
                safe_message=classification.safe_message,
            )
        failed_at = now or datetime.now(UTC)
        delay_seconds = self.retry_delay_seconds(
            task_id=task_id,
            attempt_count=attempt_count,
        )
        return ModelRetryPlan(
            category=classification.category,
            should_retry=True,
            next_retry_at=failed_at + timedelta(seconds=delay_seconds),
            safe_message=classification.safe_message,
        )

    def retry_delay_seconds(self, *, task_id: int, attempt_count: int) -> int:
        exponent = max(attempt_count - 1, 0)
        base_delay = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2**exponent),
        )
        digest = hashlib.sha256(f"{task_id}:{attempt_count}".encode()).digest()
        unit = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
        jitter_factor = 1 + ((unit * 2) - 1) * self.jitter_ratio
        return max(1, math.ceil(min(self.max_delay_seconds, base_delay * jitter_factor)))

    @classmethod
    def classify(cls, error: BaseException) -> ModelFailureClassification:
        root = cls._root_cause(error)
        if isinstance(root, (APITimeoutError, httpx.TimeoutException, TimeoutError)):
            return ModelFailureClassification(
                category=ModelTaskErrorCategory.MODEL_TIMEOUT,
                retryable=True,
                safe_message="模型调用超时",
            )
        if isinstance(root, RateLimitError) or cls._status_code(root) == 429:
            return ModelFailureClassification(
                category=ModelTaskErrorCategory.RATE_LIMITED,
                retryable=True,
                safe_message="模型服务限流",
            )
        status_code = cls._status_code(root)
        if isinstance(root, (APIConnectionError, httpx.TransportError, OSError)) or (
            status_code is not None and (status_code == 408 or status_code >= 500)
        ):
            return ModelFailureClassification(
                category=ModelTaskErrorCategory.NETWORK,
                retryable=True,
                safe_message="模型服务网络异常",
            )
        if isinstance(
            root,
            (
                StructuredOutputValidationError,
                ValidationError,
                json.JSONDecodeError,
                ValueError,
            ),
        ):
            return ModelFailureClassification(
                category=ModelTaskErrorCategory.INVALID_OUTPUT,
                retryable=True,
                safe_message="模型结构化输出无效",
            )
        return ModelFailureClassification(
            category=ModelTaskErrorCategory.INTERNAL,
            retryable=False,
            safe_message=f"模型任务发生不可重试错误（{type(root).__name__}）",
        )

    @staticmethod
    def _root_cause(error: BaseException) -> BaseException:
        current = error
        visited: set[int] = set()
        while current.__cause__ is not None and id(current) not in visited:
            visited.add(id(current))
            current = current.__cause__
        return current

    @staticmethod
    def _status_code(error: BaseException) -> int | None:
        if isinstance(error, APIStatusError):
            return error.status_code
        value = getattr(error, "status_code", None)
        return value if type(value) is int else None
