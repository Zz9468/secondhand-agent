from decimal import ROUND_HALF_UP, Decimal

from app.agent.model_observation import ProviderUsage
from app.core.config import Settings
from app.services.model_task_service import ModelUsage

_MILLION = Decimal("1000000")
_COST_QUANTUM = Decimal("0.00000001")


class ModelUsageService:
    """把提供商用量转换为带运行时价格快照的可复算任务记录。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def build(self, usage: ProviderUsage) -> ModelUsage:
        input_price = self._settings.model_input_price_per_million
        output_price = self._settings.model_output_price_per_million
        cached_price = self._settings.model_cached_input_price_per_million
        currency = self._settings.model_cost_currency
        estimated_cost: Decimal | None = None

        if (
            usage.input_tokens is not None
            and usage.output_tokens is not None
            and input_price is not None
            and output_price is not None
            and currency is not None
        ):
            cached_tokens = usage.cached_input_tokens or 0
            if cached_tokens <= usage.input_tokens and (
                cached_tokens == 0 or cached_price is not None
            ):
                uncached_tokens = usage.input_tokens - cached_tokens
                cached_cost = (
                    Decimal(cached_tokens) * cached_price
                    if cached_tokens and cached_price is not None
                    else Decimal("0")
                )
                estimated_cost = (
                    (
                        Decimal(uncached_tokens) * input_price
                        + cached_cost
                        + Decimal(usage.output_tokens) * output_price
                    )
                    / _MILLION
                ).quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)

        return ModelUsage(
            provider=usage.provider,
            model_name=usage.model_name,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            total_tokens=usage.total_tokens,
            input_price_per_million=input_price,
            output_price_per_million=output_price,
            cached_input_price_per_million=cached_price,
            estimated_cost=estimated_cost,
            cost_currency=currency,
        )
