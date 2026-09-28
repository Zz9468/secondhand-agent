from datetime import UTC, datetime, timedelta

from app.db.models import ModelTaskErrorCategory
from app.services.model_retry_policy import ModelRetryPolicy


def _policy(*, max_attempts: int = 3) -> ModelRetryPolicy:
    return ModelRetryPolicy(
        max_attempts=max_attempts,
        base_delay_seconds=2,
        max_delay_seconds=10,
        jitter_ratio=0,
        lease_seconds=30,
    )


def test_retryable_failures_use_bounded_exponential_backoff() -> None:
    policy = _policy()
    failed_at = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)

    first = policy.plan_failure(
        task_id=12,
        attempt_count=1,
        max_attempts=3,
        error=TimeoutError("provider secret must not be persisted"),
        now=failed_at,
    )
    second = policy.plan_failure(
        task_id=12,
        attempt_count=2,
        max_attempts=3,
        error=ValueError("invalid raw response"),
        now=failed_at,
    )
    exhausted = policy.plan_failure(
        task_id=12,
        attempt_count=3,
        max_attempts=3,
        error=ConnectionError("private endpoint"),
        now=failed_at,
    )

    assert first.category is ModelTaskErrorCategory.MODEL_TIMEOUT
    assert first.should_retry is True
    assert first.next_retry_at == failed_at + timedelta(seconds=2)
    assert first.safe_message == "模型调用超时"
    assert second.category is ModelTaskErrorCategory.INVALID_OUTPUT
    assert second.next_retry_at == failed_at + timedelta(seconds=4)
    assert exhausted.category is ModelTaskErrorCategory.NETWORK
    assert exhausted.should_retry is False
    assert exhausted.next_retry_at is None


def test_permanent_error_stops_without_using_the_exception_message() -> None:
    plan = _policy().plan_failure(
        task_id=7,
        attempt_count=1,
        max_attempts=3,
        error=RuntimeError("api-key=must-not-leak"),
    )

    assert plan.category is ModelTaskErrorCategory.INTERNAL
    assert plan.should_retry is False
    assert plan.next_retry_at is None
    assert "api-key" not in plan.safe_message
    assert "RuntimeError" in plan.safe_message


def test_jitter_is_deterministic_for_same_task_attempt() -> None:
    policy = ModelRetryPolicy(
        max_attempts=3,
        base_delay_seconds=10,
        max_delay_seconds=60,
        jitter_ratio=0.2,
        lease_seconds=30,
    )

    assert policy.retry_delay_seconds(task_id=42, attempt_count=2) == (
        policy.retry_delay_seconds(task_id=42, attempt_count=2)
    )
