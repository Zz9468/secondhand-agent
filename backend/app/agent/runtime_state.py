from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from app.agent.decision import NegotiationAction, NegotiationDecision
from app.agent.negotiation_memory import MemoryDelta
from app.agent.prompt_registry import load_prompt


class FinishTurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: NegotiationDecision
    stop_reason: str = Field(default="COMPLETED", max_length=80)
    memory_delta: MemoryDelta = Field(default_factory=MemoryDelta)

    def validate_non_write(self):
        if self.decision.action not in {NegotiationAction.INQUIRY, NegotiationAction.REJECT}:
            raise ValueError("finish_turn permits only consultation or rejection")

    def business_decision(self):
        return self.decision


@dataclass(frozen=True)
class RuntimeResult:
    result: object
    stop_reason: str


class AgentBudgetExceeded(RuntimeError):
    pass


def call_budget(config, purpose):
    budget = dict(config["budget"])
    if purpose in {"REPLY_REVIEW", "CONVERSATION_FINISH"} and config.get("reply_review"):
        budget["output_tokens"] = config["reply_review"]["output_tokens"]
    return budget


def call_model_parameters(config, purpose):
    params = dict(config["model"])
    if purpose in {"REPLY_REVIEW", "CONVERSATION_FINISH"} and config.get("reply_review"):
        params["thinking"] = config["reply_review"]["thinking"]
        params["thinking_budget"] = config["reply_review"]["thinking_budget"]
    return params


def freeze_config(settings) -> dict:
    conversation = settings.agent_reply_strategy == "conversation-v1"
    return {
        "schema_version": 1,
        "prompt": load_prompt("seller_react_v2" if conversation else "seller_react_v1"),
        "reply_strategy": settings.agent_reply_strategy,
        "reply_review_prompt": load_prompt("reply_review_v1") if conversation else None,
        "reply_review": {
            "thinking": settings.model_enable_thinking
            if settings.agent_reply_review_enable_thinking is None
            else settings.agent_reply_review_enable_thinking,
            "thinking_budget": 1024,
            "output_tokens": 3072,
        }
        if conversation
        else None,
        "assistant_capabilities": {
            "price_discussion": True,
            "offer_evaluation": True,
            "seller_contact": False,
            "photo_capture": False,
            "formal_writes_require_tools": True,
            "unconfirmed_dispatch_promise": False,
        }
        if conversation
        else {},
        "summary_prompt": load_prompt("history_summary_v1"),
        "memory_prompt": load_prompt("negotiation_memory_v1"),
        "toolset_version": "seller-tools-v2" if conversation else "seller-tools-v1",
        "context_strategy_version": "incremental-v1",
        "ending_strategy": "structured-finish-v2",
        "memory_catchup_strategy": "contiguous-prefix-v1",
        "memory_enabled": settings.agent_memory_enabled,
        "prices": {
            "input": str(settings.model_input_price_per_million)
            if settings.model_input_price_per_million is not None
            else None,
            "output": str(settings.model_output_price_per_million)
            if settings.model_output_price_per_million is not None
            else None,
            "cached": str(settings.model_cached_input_price_per_million)
            if settings.model_cached_input_price_per_million is not None
            else None,
            "currency": settings.model_cost_currency,
        },
        "model": {
            "provider": settings.model_provider,
            "endpoint": settings.model_base_url,
            "name": settings.model_name,
            "temperature": settings.model_temperature,
            "timeout": settings.model_timeout_seconds,
            "thinking": settings.model_enable_thinking,
            "thinking_budget": settings.model_thinking_budget,
        },
        "budget": {
            "context_window": settings.agent_context_window,
            "input_tokens": settings.agent_input_token_budget,
            "output_tokens": settings.agent_output_token_reserve,
            "safety": settings.agent_token_safety_margin,
            "growth_reserve": settings.agent_initial_tool_growth_reserve,
            "compression_trigger": settings.agent_compression_trigger_ratio,
            "compression_target": settings.agent_compression_target_ratio,
            "tool_result_tokens": settings.agent_tool_result_token_limit,
            "model_steps": settings.agent_max_model_steps,
            "total_calls": settings.agent_max_total_model_calls,
            "maintenance_calls": settings.agent_max_maintenance_calls,
            "tool_calls": settings.agent_max_tool_calls,
            "wall_seconds": settings.agent_max_wall_seconds,
            "total_tokens": settings.agent_max_total_tokens,
            "estimated_cost": str(settings.agent_max_estimated_cost)
            if settings.agent_max_estimated_cost
            else None,
        },
    }
