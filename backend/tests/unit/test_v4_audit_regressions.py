import json
from copy import deepcopy
from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.context_builder import ContextBuilder
from app.agent.message_protocol import ContextState, MessageProtocolValidator, StreamRecord
from app.agent.negotiation_memory import MemoryDelta, MemoryUpdater
from app.agent.runtime import SellerAgentRuntime
from app.agent.runtime_state import AgentBudgetExceeded, freeze_config
from app.core.config import get_settings
from evaluation.dynamic_buyer import BuyerAction
from evaluation.dynamic_report import call_calibration, trace_metrics, write_report
from evaluation.dynamic_runner import DynamicCallTracker, run_episode
from evaluation.environment import EvaluationEnvironment
from evaluation.production_driver import ProductionDriver
from evaluation.scenarios import load_scenarios


def finish(call_id="finish"):
    return native(
        "finish_turn",
        {
            "decision": {
                "action": "INQUIRY",
                "dialogue_acts": [{"kind": "GENERAL", "subject": "GENERAL"}],
                "reason": "咨询",
                "reply": "请继续说明需求。",
            }
        },
        call_id,
    )


def native(name, args=None, call_id="read"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}])


class SequenceProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.inputs = []

    def invoke_agent(self, messages, tools, config, *, purpose):
        MessageProtocolValidator.validate(messages)
        self.inputs.append(deepcopy(messages))
        return self.responses.pop(0)


def driver(provider):
    config = freeze_config(
        get_settings().model_copy(
            update={
                "agent_reply_strategy": "legacy-v1",
                "model_enable_thinking": False,
                "agent_output_token_reserve": 1024,
            }
        )
    )
    config["budget"]["estimated_cost"] = None
    return ProductionDriver(
        EvaluationEnvironment(load_scenarios().scenarios[0]),
        config,
        "G4",
        DynamicCallTracker(provider, config),
    )


@pytest.mark.parametrize("terms", [None, [], "invalid"])
def test_rejected_tool_arguments_do_not_crash_memory_recording(terms):
    provider = SequenceProvider(
        native(
            "evaluate_offer",
            {"price": "2700", "shipping_paid_by": "buyer", "additional_terms": terms},
        ),
        finish(),
    )
    result = driver(provider).run_turn(BuyerAction(action="ASK", message="介绍一下商品"))
    assert result.outcome.value == "INFORMATIONAL"
    receipt = next(m for m in provider.inputs[-1] if isinstance(m, ToolMessage))
    assert json.loads(receipt.content)["ok"] is False


def test_tool_memory_is_available_to_the_next_model_call():
    provider = SequenceProvider(
        native("evaluate_offer", {"price": "2700", "shipping_paid_by": "buyer"}), finish()
    )
    instance = driver(provider)
    instance.run_turn(BuyerAction(action="ASK", message="谈谈交易条件"))
    facts = json.loads(provider.inputs[-1][1].content.split("\n", 1)[1])
    assert any(e["kind"] == "ATTEMPTED_OPTION" for e in facts["memory"]["entries"])
    assert facts["memory"] == instance.context["memory"]


def test_initial_growth_reserve_is_optional_when_pinned_context_fits():
    config = freeze_config(
        get_settings().model_copy(
            update={
                "agent_reply_strategy": "legacy-v1",
                "model_enable_thinking": False,
                "agent_output_token_reserve": 1024,
            }
        )
    )
    config["prompt"]["text"] = "卖家助手"
    config["budget"]["input_tokens"] = 1024
    rows = [StreamRecord(1, "BUYER_INPUT", {"content": "介绍商品"}, visibility="PUBLIC")]
    messages, manifest, _ = ContextBuilder(config).build(
        rows, ContextState(), {}, [], current_input_seq=1, growth_reserve=2048
    )
    assert manifest["estimated_input_tokens"] <= 1024
    assert manifest["initial_growth_reserve_available"] < 2048
    assert messages[-1].content == "介绍商品"


def test_long_native_call_id_is_repaired_before_persistence():
    provider = SequenceProvider(native("get_product_info", call_id="x" * 193), finish())
    instance = driver(provider)
    result = instance.run_turn(BuyerAction(action="ASK", message="介绍商品"))
    assert result.outcome.value == "INFORMATIONAL"
    assert instance.stream[1].kind == "MODEL_CANDIDATE"
    assert not any(
        m.tool_call_id == "x" * 193 for m in provider.inputs[-1] if isinstance(m, ToolMessage)
    )


def test_history_search_does_not_nest_artifacts_from_earlier_searches():
    provider = SequenceProvider(
        native("search_conversation_history", {"query": "", "limit": 12}, "search-1"),
        native("search_conversation_history", {"query": "", "limit": 12}, "search-2"),
        finish(),
    )
    instance = driver(provider)
    instance.run_turn(BuyerAction(action="ASK", message="介绍商品"))
    results = [r for r in instance.stream if r.kind == "TOOL_RESULT"]
    history = results[1].payload["message"]["data"]["artifact"]["full_result"]
    assert "artifact" not in json.dumps(history, ensure_ascii=False)
    assert "usage_metadata" not in json.dumps(history, ensure_ascii=False)


def test_context_source_catalog_identifies_buyer_and_tool_messages():
    config = freeze_config(
        get_settings().model_copy(
            update={
                "agent_reply_strategy": "legacy-v1",
                "model_enable_thinking": False,
                "agent_output_token_reserve": 1024,
            }
        )
    )
    rows = [StreamRecord(7, "BUYER_INPUT", {"content": "我能自提"}, visibility="PUBLIC")]
    messages, _, _ = ContextBuilder(config).build(rows, ContextState(), {}, [], current_input_seq=7)
    catalog = json.loads(messages[-2].content.split("\n", 1)[1])
    assert catalog["current_input_seq"] == 7
    assert catalog["sources"] == [{"seq": 7, "kind": "BUYER_INPUT", "role": "user"}]


def test_older_memory_evidence_cannot_restore_a_replaced_preference():
    rows = [
        StreamRecord(1, "BUYER_INPUT", {"content": "我能自提"}),
        StreamRecord(10, "BUYER_INPUT", {"content": "我改成快递"}),
    ]
    current = {
        "entries": [
            {
                "kind": "BUYER_STATEMENT",
                "field": "delivery",
                "value": "快递",
                "source_seqs": [10],
                "status": "ACTIVE",
                "confidence": "EXPLICIT",
            }
        ]
    }
    delta = MemoryDelta.model_validate(
        {
            "entries": [
                {
                    "kind": "BUYER_STATEMENT",
                    "field": "delivery",
                    "value": "自提",
                    "source_seqs": [1],
                }
            ]
        }
    )
    assert MemoryUpdater.apply(current, delta, rows) == current


def test_distinct_open_questions_in_same_subject_remain_active():
    rows = [StreamRecord(1, "BUYER_INPUT", {"content": "运费多少？谁承担？"})]
    questions = [
        {"kind": "OPEN_QUESTION", "field": "shipping", "value": value, "source_seqs": [1]}
        for value in ("运费多少？", "谁承担？")
    ]
    memory = MemoryUpdater.apply({}, MemoryDelta.model_validate({"entries": questions}), rows)
    assert all(e["status"] == "ACTIVE" for e in memory["entries"])
    resolved = {**questions[0], "status": "RESOLVED"}
    memory = MemoryUpdater.apply(memory, MemoryDelta.model_validate({"entries": [resolved]}), rows)
    assert any(e["value"] == "谁承担？" and e["status"] == "ACTIVE" for e in memory["entries"])


def test_last_seller_turn_can_be_confirmed(monkeypatch):
    from evaluation.schemas import OfferSpec

    case = next(s for s in load_scenarios().scenarios if s.scenario_id == "inquiry_01")

    class Buyer:
        def __init__(self, tracker):
            pass

        def next_action(self, view, goal, profile):
            if view["turn"] == 1:
                return BuyerAction(
                    action="OFFER",
                    message="正式报价",
                    offer=OfferSpec(price="2900", shipping_paid_by="buyer"),
                )
            return BuyerAction(
                action="CONFIRM", message="确认", offer_id=view["current_offer"]["id"]
            )

    monkeypatch.setattr("evaluation.dynamic_runner.ModelDynamicBuyer", Buyer)
    config = freeze_config(
        get_settings().model_copy(
            update={
                "agent_reply_strategy": "legacy-v1",
                "model_enable_thinking": False,
                "agent_output_token_reserve": 1024,
            }
        )
    )
    tracker = DynamicCallTracker(SequenceProvider(finish()), config)
    result = run_episode(
        case,
        "G4",
        config,
        tracker,
        seed=1,
        trial=0,
        buyer_mode="qwen",
        profiles={"defaults": {"patience": 2, "max_turns": 2}, "category_styles": {}},
    )
    assert result["status"] == "AGREED"
    assert len([r for r in result["stream"] if r["kind"] == "BUYER_INPUT"]) == 2


def test_memory_failure_on_earlier_turn_is_counted_after_deterministic_finish(monkeypatch):
    from evaluation.schemas import OfferSpec

    case = next(s for s in load_scenarios().scenarios if s.scenario_id == "inquiry_01")
    bad = finish()
    bad.tool_calls[0]["args"]["memory_delta"] = {
        "entries": [
            {"kind": "BUYER_STATEMENT", "field": "delivery", "value": "快递", "source_seqs": [999]}
        ]
    }

    class Buyer:
        def __init__(self, tracker):
            pass

        def next_action(self, view, goal, profile):
            if view["turn"] == 1:
                return BuyerAction(
                    action="OFFER",
                    message="正式报价",
                    offer=OfferSpec(price="2900", shipping_paid_by="buyer"),
                )
            return BuyerAction(
                action="CONFIRM", message="确认", offer_id=view["current_offer"]["id"]
            )

    monkeypatch.setattr("evaluation.dynamic_runner.ModelDynamicBuyer", Buyer)
    config = freeze_config(
        get_settings().model_copy(
            update={
                "agent_reply_strategy": "legacy-v1",
                "model_enable_thinking": False,
                "agent_output_token_reserve": 1024,
            }
        )
    )
    result = run_episode(
        case,
        "G4",
        config,
        DynamicCallTracker(SequenceProvider(bad), config),
        seed=1,
        trial=0,
        buyer_mode="qwen",
    )
    assert result["status"] == "AGREED"
    assert result["memory_update_failures"] == 1
    assert result["memory_update_failure_events"] == 1
    assert result["memory_update_failure_details"][0]["phase"] == "FINISH_DELTA"


def test_calibration_uses_recorded_output_reserve():
    calls = [
        {
            "usage": {"input_tokens": 500},
            "reserved_tokens": 628,
            "request": {"max_output_tokens": 128},
        }
    ]
    assert call_calibration(calls)["minimum_estimate_to_actual_input_ratio"] == 1


def test_call_reservation_and_batch_total_agree_after_underestimate():
    config = freeze_config(
        get_settings().model_copy(
            update={
                "agent_reply_strategy": "legacy-v1",
                "model_enable_thinking": False,
                "agent_output_token_reserve": 1024,
            }
        )
    )
    config["prices"] = {"input": "1", "output": "1", "cached": "0", "currency": "CNY"}
    provider = SequenceProvider(
        AIMessage(
            content="done",
            usage_metadata={"input_tokens": 9000, "output_tokens": 128, "total_tokens": 9128},
        )
    )
    tracker = DynamicCallTracker(provider, config)
    tracker([HumanMessage(content="x")], [], purpose="REACT", manifest={})
    call = tracker.calls[0]
    assert tracker.reserved_cost == Decimal(call["reserved_cost"])
    assert tracker.reserved_tokens == call["reserved_tokens"]
    assert Decimal(call["reserved_cost"]) >= Decimal(call["estimated_cost"])
    assert call_calibration(tracker.calls)["underestimated_calls"] == 1


def test_runtime_actual_usage_survives_cached_response_recovery():
    config = freeze_config(
        get_settings().model_copy(
            update={
                "agent_reply_strategy": "legacy-v1",
                "model_enable_thinking": False,
                "agent_output_token_reserve": 1024,
            }
        )
    )
    config["prices"] = {"input": "1", "output": "1", "cached": "0", "currency": "CNY"}
    config["budget"].update(output_tokens=128, estimated_cost="0.005")
    response = AIMessage(
        content="done",
        usage_metadata={"input_tokens": 9000, "output_tokens": 128, "total_tokens": 9128},
    )

    class Backend:
        initial_context = {}
        checkpoint = {}
        cache = None

        def __init__(self):
            self.config = config

        def save_checkpoint(self, cp):
            self.persisted = deepcopy(cp)

        def cached_response(self, purpose, cp):
            return self.cache

        def call_model(self, *args):
            self.before_response = deepcopy(self.persisted)
            return response

    backend = Backend()
    runtime = SellerAgentRuntime(backend)
    runtime.call([HumanMessage(content="x")], [], purpose="REACT", manifest={})
    assert runtime.cp["reserved_tokens"] == 9128
    assert Decimal(runtime.cp["reserved_cost"]) == Decimal("0.009128")
    # Restore the durable reservation immediately before response reconciliation.
    backend.checkpoint, backend.cache = backend.before_response, response
    resumed = SellerAgentRuntime(backend)
    resumed.call([HumanMessage(content="x")], [], purpose="REACT", manifest={})
    resumed.call([HumanMessage(content="x")], [], purpose="REACT", manifest={})
    assert resumed.cp["calls"] == 1
    assert Decimal(resumed.cp["reserved_cost"]) == Decimal("0.009128")
    backend.cache = None
    with pytest.raises(AgentBudgetExceeded, match="COST_BUDGET"):
        resumed.call([HumanMessage(content="x")], [], purpose="REACT", manifest={})


def test_trace_metrics_distinguish_internal_json_envelope_from_native_calls():
    run = {
        "group": "G4",
        "run_id": "example",
        "final_memory": {},
        "stream": [
            {
                "seq": 1,
                "kind": "TOOL_CALL",
                "task_id": 1,
                "payload": {
                    "tool_call_origin": "STRUCTURED_FINISH",
                    "message": {
                        "data": {
                            "tool_calls": [{"name": "finish_turn", "args": {}, "id": "finish"}]
                        }
                    },
                },
            }
        ],
    }
    metrics = trace_metrics([run])["G4"]
    assert metrics["native_tool_calls"] == 0
    assert metrics["internal_finish_calls"] == 1


def test_report_supports_a_batch_without_feasible_cases(tmp_path):
    from evaluation.dynamic_models import ScriptedAgentProvider

    config = freeze_config(
        get_settings().model_copy(
            update={
                "agent_reply_strategy": "legacy-v1",
                "model_enable_thinking": False,
                "agent_output_token_reserve": 1024,
            }
        )
    )
    case = next(
        s for s in load_scenarios().scenarios if s.scenario_id == "terms_02_warranty"
    ).model_copy(deep=True)
    case.buyer_goal.maximum_price = Decimal("2600")
    tracker = DynamicCallTracker(ScriptedAgentProvider(), config)
    run = run_episode(case, "G4", config, tracker, seed=1, trial=0)
    assert not run["feasible"]
    (tmp_path / "runs.jsonl").write_text(json.dumps(run) + "\n", encoding="utf-8")
    (tmp_path / "calls.jsonl").write_text(
        "\n".join(json.dumps(c) for c in tracker.calls), encoding="utf-8"
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"config": config, "scenario_set_version": "test"}), encoding="utf-8"
    )
    write_report(tmp_path)
    assert "| G4 | — |" in (tmp_path / "report.md").read_text(encoding="utf-8")
