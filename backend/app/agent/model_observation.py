from dataclasses import dataclass
from typing import Generic, TypeVar

from langchain_core.callbacks import UsageMetadataCallbackHandler

ValueT = TypeVar("ValueT")


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    provider: str
    model_name: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ObservedProviderResult(Generic[ValueT]):
    value: ValueT
    usage: ProviderUsage


def usage_from_callback(
    callback: UsageMetadataCallbackHandler,
    *,
    provider: str,
    fallback_model_name: str,
) -> ProviderUsage:
    input_tokens = 0
    output_tokens = 0
    cached_input_tokens = 0
    total_tokens = 0
    has_usage = False
    model_names: list[str] = []

    for model_name, usage in callback.usage_metadata.items():
        has_usage = True
        model_names.append(str(model_name))
        input_tokens += int(usage.get("input_tokens", 0))
        output_tokens += int(usage.get("output_tokens", 0))
        total_tokens += int(usage.get("total_tokens", 0))
        input_details = usage.get("input_token_details") or {}
        cached_input_tokens += int(input_details.get("cache_read", 0))

    return ProviderUsage(
        provider=provider[:50] or "unknown",
        model_name=(model_names[-1] if model_names else fallback_model_name)[:100]
        or "unreported",
        input_tokens=input_tokens if has_usage else None,
        output_tokens=output_tokens if has_usage else None,
        cached_input_tokens=cached_input_tokens if has_usage else None,
        total_tokens=total_tokens if has_usage else None,
    )
