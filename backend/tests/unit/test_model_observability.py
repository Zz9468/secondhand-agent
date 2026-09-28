from decimal import Decimal

from langchain_core.callbacks import UsageMetadataCallbackHandler

from app.agent.model_observation import ProviderUsage, usage_from_callback
from app.core.config import Settings
from app.observability import langsmith_sink as sink_module
from app.observability.langsmith_sink import LangSmithTraceSink
from app.services.model_usage_service import ModelUsageService


def test_usage_callback_aggregates_models_and_cached_tokens() -> None:
    callback = UsageMetadataCallbackHandler()
    callback.usage_metadata.update(
        {
            "qwen-plus": {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_token_details": {"cache_read": 25},
            },
            "qwen-plus-retry": {
                "input_tokens": 40,
                "output_tokens": 10,
                "total_tokens": 50,
            },
        }
    )

    usage = usage_from_callback(
        callback,
        provider="qwen",
        fallback_model_name="fallback",
    )

    assert usage.input_tokens == 140
    assert usage.output_tokens == 30
    assert usage.cached_input_tokens == 25
    assert usage.total_tokens == 170
    assert usage.model_name == "qwen-plus-retry"


def test_cost_uses_runtime_price_snapshot_and_cached_rate() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite://",
        model_input_price_per_million=Decimal("2"),
        model_output_price_per_million=Decimal("8"),
        model_cached_input_price_per_million=Decimal("0.5"),
        model_cost_currency="CNY",
    )

    usage = ModelUsageService(settings).build(
        ProviderUsage(
            provider="qwen",
            model_name="qwen-plus",
            input_tokens=100,
            output_tokens=25,
            cached_input_tokens=20,
            total_tokens=125,
        )
    )

    assert usage.estimated_cost == Decimal("0.00037000")
    assert usage.input_price_per_million == Decimal("2")
    assert usage.cached_input_price_per_million == Decimal("0.5")
    assert usage.cost_currency == "CNY"


def test_missing_prices_preserve_usage_without_inventing_cost() -> None:
    settings = Settings(_env_file=None, database_url="sqlite://")

    usage = ModelUsageService(settings).build(
        ProviderUsage(
            provider="qwen",
            model_name="qwen-plus",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
        )
    )

    assert usage.total_tokens == 15
    assert usage.estimated_cost is None
    assert usage.cost_currency is None


def test_langsmith_sink_only_sends_redacted_payload(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            calls.append({"client": kwargs})

        def create_run(self, **kwargs: object) -> None:
            calls.append({"run": kwargs})

    monkeypatch.setattr(sink_module, "Client", FakeClient)
    settings = Settings(
        _env_file=None,
        database_url="sqlite://",
        observability_langsmith_enabled=True,
        observability_langsmith_api_key="trace-secret",
    )
    sink = LangSmithTraceSink(settings)

    sink.emit(
        {
            "event_id": "b5598552-63f7-4b44-ac7b-4dd1229743f3",
            "event_type": "MODEL_CALL_COMPLETED",
            "outcome": "SUCCESS",
            "created_at": "2026-09-28T12:00:00",
            "correlation_id": "1b72cb50-23c8-43db-8c0b-867e78b3a232",
        }
    )

    run = calls[1]["run"]
    assert isinstance(run, dict)
    assert run["inputs"] == {"redacted": True}
    assert run["outputs"] == {"redacted": True, "outcome": "SUCCESS"}
    client = calls[0]["client"]
    assert isinstance(client, dict)
    assert client["hide_inputs"] is True
    assert client["hide_outputs"] is True
