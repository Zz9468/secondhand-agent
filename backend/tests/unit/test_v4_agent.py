import json
from copy import deepcopy
from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.context_builder import ContextBuilder
from app.agent.context_compressor import ContextCompressor
from app.agent.message_protocol import (
    ContextState,
    MessageProtocolValidator,
    StreamRecord,
    encode_message,
    wire_message,
)
from app.agent.negotiation_memory import MemoryDelta, MemoryUpdater
from app.agent.runtime_state import AgentBudgetExceeded, freeze_config
from app.agent.token_budget import TokenBudgetManager
from app.agent.tools.gateway import batch_error, visible_tool_result
from app.core.config import get_settings
from evaluation.dynamic_buyer import BuyerAction, ScriptedDynamicBuyer, validate_action
from evaluation.dynamic_models import ScriptedAgentProvider
from evaluation.dynamic_runner import DynamicCallTracker, load_profiles, run_batch, select_balanced
from evaluation.environment import EvaluationEnvironment
from evaluation.production_driver import ProductionDriver
from evaluation.scenarios import load_scenarios


@pytest.fixture
def config():
    return freeze_config(
        get_settings().model_copy(
            update={
                "agent_reply_strategy": "legacy-v1",
                "model_enable_thinking": False,
                "agent_output_token_reserve": 1024,
            }
        )
    )


def record(seq, kind, message):
    payload = (
        {"content": message} if isinstance(message, str) else {"message": encode_message(message)}
    )
    return StreamRecord(
        seq,
        kind,
        payload,
        visibility="PUBLIC" if kind in {"BUYER_INPUT", "PUBLIC_REPLY"} else "INTERNAL",
    )


def ending(call_id="end"):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": call_id,
                "name": "finish_turn",
                "args": {
                    "decision": {
                        "action": "INQUIRY",
                        "dialogue_acts": [{"kind": "GENERAL", "subject": "GENERAL"}],
                        "reason": "咨询",
                        "reply": "欢迎继续咨询。",
                    }
                },
                "type": "tool_call",
            }
        ],
    )


@pytest.mark.parametrize(
    "messages",
    [
        [ToolMessage(content="x", tool_call_id="missing")],
        [
            AIMessage(content="", tool_calls=[{"id": "a", "name": "x", "args": {}}]),
            HumanMessage(content="x"),
        ],
        [AIMessage(content="", tool_calls=[{"id": "a", "name": "x", "args": {}}])],
    ],
)
def test_protocol_rejects_orphan_or_incomplete_exchanges(messages):
    with pytest.raises(ValueError):
        MessageProtocolValidator.validate(messages)


def test_multiple_calls_are_an_indivisible_group():
    calls = AIMessage(
        content="", tool_calls=[{"id": x, "name": "query", "args": {}} for x in ("a", "b")]
    )
    rows = [
        record(1, "BUYER_INPUT", "hello"),
        record(2, "TOOL_CALL", calls),
        record(3, "TOOL_RESULT", ToolMessage(content="1", tool_call_id="a")),
        record(4, "TOOL_RESULT", ToolMessage(content="2", tool_call_id="b")),
    ]
    assert [[r.seq for r in g] for g in MessageProtocolValidator.groups(rows)] == [[1], [2, 3, 4]]
    MessageProtocolValidator.validate(
        [
            HumanMessage(content="hello"),
            calls,
            ToolMessage(content="1", tool_call_id="a"),
            ToolMessage(content="2", tool_call_id="b"),
        ]
    )


def context_limit_with_history_space(config, history_tokens):
    # Keep testing compression pressure as the production prompt evolves.
    _, manifest, _ = ContextBuilder(config).build([], ContextState(), {}, [], current_input_seq=0)
    config["budget"]["input_tokens"] = manifest["estimated_input_tokens"] + history_tokens


def test_full_history_and_incremental_summary_keep_raw_evidence(config):
    context_limit_with_history_space(config, 1500)
    rows = [
        record(i, "BUYER_INPUT" if i % 2 else "PUBLIC_REPLY", "历史问题" * 50) for i in range(1, 42)
    ]
    original = deepcopy(rows)
    calls = []

    def summarize(messages, tools, **kwargs):
        calls.append(kwargs)
        source = json.loads(messages[-1].content)["records"]
        return AIMessage(
            content=json.dumps(
                {
                    "text": "已讨论商品细节，保留最新问题。",
                    "source_seqs": [r["seq"] for r in source],
                }
            )
        )

    compressor = ContextCompressor(config, summarize)
    messages, manifest, state = ContextBuilder(config).build(
        rows, ContextState(), {}, [], current_input_seq=41, compressor=compressor.compress
    )
    assert len(rows) > 20 and rows == original
    assert manifest["estimated_input_tokens"] <= config["budget"]["input_tokens"]
    assert 41 in manifest["source_seqs"] and 41 not in state.summary_sources
    assert (
        sum(
            isinstance(m, HumanMessage) and m.content == rows[-1].payload["content"]
            for m in messages
        )
        >= 1
    )
    previous_calls = len(calls)
    ContextBuilder(config).build(rows, state, {}, [], current_input_seq=41)
    assert len(calls) == previous_calls


def test_compaction_of_current_tool_exchanges_keeps_pinned_buyer(config):
    context_limit_with_history_space(config, 2500)
    rows = [record(1, "BUYER_INPUT", "当前诉求：自提")]
    for i in range(5):
        call_id = str(i)
        rows.extend(
            [
                record(
                    len(rows) + 1,
                    "TOOL_CALL",
                    AIMessage(
                        content="",
                        tool_calls=[{"id": call_id, "name": "get_product_info", "args": {}}],
                    ),
                ),
                record(
                    len(rows) + 2,
                    "TOOL_RESULT",
                    ToolMessage(content="公开事实" * 100, tool_call_id=call_id),
                ),
            ]
        )
    provider = ScriptedAgentProvider()

    def call(messages, tools, **kwargs):
        return provider.invoke_agent(messages, tools, config, purpose="SUMMARY")

    messages, manifest, state = ContextBuilder(config).build(
        rows,
        ContextState(),
        {},
        [],
        current_input_seq=1,
        compressor=ContextCompressor(config, call).compress,
    )
    assert 1 in manifest["source_seqs"] and state.covered_through == 0
    assert state.summary_sources
    MessageProtocolValidator.validate(messages)


def test_one_large_pinned_input_stops_before_call(config):
    config["budget"]["input_tokens"] = 1500
    with pytest.raises(AgentBudgetExceeded, match="PINNED_CONTEXT"):
        ContextBuilder(config).build(
            [record(1, "BUYER_INPUT", "长内容" * 4000)], ContextState(), {}, [], current_input_seq=1
        )


def test_fallback_omits_whole_exchanges_when_summary_unavailable(config):
    context_limit_with_history_space(config, 1000)
    rows = [record(i, "BUYER_INPUT", "旧输入" * 100) for i in range(1, 21)]
    rows.append(record(21, "BUYER_INPUT", "新输入"))
    messages, manifest, _ = ContextBuilder(config).build(
        rows, ContextState(), {}, [], current_input_seq=21
    )
    assert manifest["omitted_source_seqs"] and 21 in manifest["source_seqs"]
    MessageProtocolValidator.validate(messages)


def test_memory_catchup_trims_tail_and_keeps_contiguous_coverage(config):
    from app.agent.runtime import SellerAgentRuntime

    config["budget"]["input_tokens"] = 6000

    class Backend:
        checkpoint = {}
        initial_context = {}
        current_input_seq = 3

        def __init__(self):
            self.config = config
            self.sources = []

        def cached_response(self, purpose, checkpoint):
            return None

        def save_checkpoint(self, checkpoint):
            self.saved = deepcopy(checkpoint)

        def call_model(self, messages, tools, purpose, manifest, checkpoint):
            self.sources = manifest["source_seqs"]
            return AIMessage(content='{"entries":[]}')

    backend = Backend()
    runtime = SellerAgentRuntime(backend)
    runtime._refresh_memory(
        [
            record(1, "BUYER_INPUT", "我能自提"),
            record(2, "BUYER_INPUT", "很长的历史输入" * 4000),
            record(3, "BUYER_INPUT", "当前输入"),
        ]
    )
    assert backend.sources == [1]
    assert runtime.cp["memory_covered_through"] == 1
    assert runtime.cp["memory_refresh_incomplete"]


@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "BUYER_STATEMENT", "field": "delivery", "value": "快递", "source_seqs": [1]},
        {"kind": "BUYER_STATEMENT", "field": "delivery", "value": "自提", "source_seqs": [99]},
        {"kind": "HYPOTHESIS", "field": "budget", "value": "3000", "source_seqs": [1]},
        {"kind": "ATTEMPTED_OPTION", "field": "price", "value": "2800", "source_seqs": [1]},
    ],
)
def test_memory_rejects_guessed_or_foreign_evidence(entry):
    with pytest.raises(ValueError):
        MemoryUpdater.apply(
            {},
            MemoryDelta.model_validate({"entries": [entry]}),
            [record(1, "BUYER_INPUT", "我能自提")],
        )


def test_changed_condition_replaces_previous_and_questions_are_distinct():
    sources = [
        record(1, "BUYER_INPUT", "我能自提"),
        record(2, "BUYER_INPUT", "改成快递"),
        record(3, "BUYER_INPUT", "能够自提吗？"),
    ]

    def delta(value, seq):
        return MemoryDelta.model_validate(
            {
                "entries": [
                    {
                        "kind": "BUYER_STATEMENT",
                        "field": "delivery",
                        "value": value,
                        "source_seqs": [seq],
                    }
                ]
            }
        )

    memory = MemoryUpdater.apply({}, delta("自提", 1), sources)
    memory = MemoryUpdater.apply(memory, delta("快递", 2), sources)
    assert [r["status"] for r in memory["entries"]] == ["REPLACED", "ACTIVE"]
    with pytest.raises(ValueError, match="question"):
        MemoryUpdater.apply(memory, delta("自提", 3), sources)


def test_wire_budget_excludes_large_local_artifacts():
    message = ToolMessage(
        content='{"ok": true}', tool_call_id="a", artifact={"full_result": "x" * 100000}
    )
    without = ToolMessage(content=message.content, tool_call_id="a")
    assert TokenBudgetManager.estimate([message]) == TokenBudgetManager.estimate([without])
    assert "artifact" not in wire_message(message)
    visible = visible_tool_result(
        {"ok": True, "records": [{"content": "x" * 4000} for _ in range(10)]}, 1000
    )
    assert visible["artifact_available"] and len(visible["records"]) < 10


@pytest.mark.parametrize(
    "names", [["accept_offer", "get_product_info"], ["accept_offer", "finish_turn"], ["unknown"]]
)
def test_invalid_tool_batches_have_no_authorized_ending(names):
    assert batch_error([{"name": name} for name in names])


def test_batch_budget_reservation_blocks_rpc_and_concurrency_is_accounted(config):
    tracker = DynamicCallTracker(ScriptedAgentProvider(), config, max_calls=0)
    with pytest.raises(AgentBudgetExceeded):
        tracker([HumanMessage(content="hello")], [], purpose="REACT", manifest={})
    assert tracker.calls == []
    tracker = DynamicCallTracker(ScriptedAgentProvider(), config)
    runs = run_batch(
        select_balanced(load_scenarios().scenarios, 7), config, tracker, repeats=1, workers=3
    )
    assert len(runs) == 35 and len({r["run_id"] for r in runs}) == 35
    assert all(c["run_id"] in {r["run_id"] for r in runs} for c in tracker.calls)
    assert tracker.reserved_tokens == sum(c["reserved_tokens"] for c in tracker.calls)


def test_dynamic_buyer_validates_confirmation_and_budget(config):
    scenario = load_scenarios().scenarios[0]
    env = EvaluationEnvironment(scenario)
    with pytest.raises(ValueError):
        validate_action(
            BuyerAction(action="CONFIRM", message="接受", offer_id=99), env, scenario.buyer_goal
        )
    with pytest.raises(ValueError):
        validate_action(
            BuyerAction(
                action="OFFER",
                message="报价",
                offer={
                    "price": str(scenario.buyer_goal.maximum_price + 1),
                    "shipping_paid_by": "buyer",
                },
            ),
            env,
            scenario.buyer_goal,
        )
    assert not env.offers
    buyer = ScriptedDynamicBuyer(1)
    profile = {"style": "reasonable", "patience": 4, "max_turns": 6}
    assert (
        buyer.next_action({"turn": 4, "messages": []}, scenario.buyer_goal, profile).action
        == "CLOSE"
    )
    assert set(load_profiles()["category_styles"].values()) == {
        "reasonable",
        "persistent_bargainer",
        "hesitant",
        "changing_terms",
        "fact_focused",
        "prompt_injection",
    }


def test_seller_request_excludes_hidden_goal_and_private_policy(config):
    scenario = load_scenarios().scenarios[0].model_copy(deep=True)
    scenario.buyer_goal.maximum_price = Decimal("2847.23")
    tracker = DynamicCallTracker(ScriptedAgentProvider(), config)
    driver = ProductionDriver(EvaluationEnvironment(scenario), config, "G4", tracker)
    driver.run_turn(BuyerAction(action="ASK", message="商品成色如何？"))
    requests = json.dumps([c["request"] for c in tracker.calls], ensure_ascii=False)
    assert "2847.23" not in requests
    assert "minimum_net_price" not in requests and "maximum_price" not in requests
