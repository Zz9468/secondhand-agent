import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from app.agent.message_protocol import MessageProtocolValidator
from app.db.models import (
    AgentCallSnapshot,
    AgentMessage,
    AgentSessionContext,
    Message,
    ModelExecutionTask,
)
from app.services.chat_service import ChatService
from app.services.errors import ModelDecisionError
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration


def call(name, args=None, call_id="call-1"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}],
        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    )


def finish(memory=None):
    return {
        "decision": {
            "action": "INQUIRY",
            "reason": "咨询",
            "reply": "请告诉我您关注哪些细节。",
            "dialogue_acts": [{"kind": "GENERAL", "subject": "GENERAL"}],
        },
        "memory_delta": {"entries": memory or []},
    }


class ScriptProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.inputs = []
        self.purposes = []
        self.tool_inputs = []

    def decide(self, request):
        raise AssertionError("V4 must use native tools")

    def invoke_agent(self, messages, tools, config, *, purpose="REACT"):
        MessageProtocolValidator.validate(messages)
        self.inputs.append(deepcopy(messages))
        self.purposes.append(purpose)
        self.tool_inputs.append(deepcopy(tools))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def v4(monkeypatch):
    from app.core.config import get_settings

    settings = get_settings().model_copy(
        update={
            "agent_runtime": "V4",
            "agent_v4_session_ids": None,
            "agent_reply_strategy": "legacy-v1",
            "model_enable_thinking": False,
            "agent_output_token_reserve": 1024,
        }
    )
    monkeypatch.setattr("app.services.chat_service.get_settings", lambda: settings)
    return settings


def test_negotiability_is_informational_and_does_not_create_an_offer(service_session_factory, v4):
    from app.db.models import NegotiationSession, Offer

    sid, buyer = create_negotiation(service_session_factory)
    plan = finish()
    plan["decision"].update(
        dialogue_acts=[{"kind": "ASK_FACT", "subject": "PRICE_NEGOTIATION"}],
        reply="当然支持讲价，您可以提出您的心理价位。",
    )
    provider = ScriptProvider(call("finish_turn", plan))
    result = ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="negotiability", content="是否支持讲价？"
    )
    assert result.outcome == "INFORMATIONAL"
    assert "可以讨论价格" in result.agent_message.content
    assert "评估后确认" in result.agent_message.content
    with service_session_factory() as db:
        assert db.scalar(select(Offer).where(Offer.session_id == sid)) is None
        assert db.get(NegotiationSession, sid).round_count == 0


def test_native_loop_persists_protocol_and_public_projection(service_session_factory, v4):
    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(
        call("get_product_info"),
        call("finish_turn", finish(), "call-2"),
    )
    turn = ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="v4-read", content="介绍一下商品"
    )
    assert turn.outcome == "INFORMATIONAL"
    assert len(provider.inputs) == 2
    assert all(
        sum(m.type == "human" and m.content == "介绍一下商品" for m in messages) == 1
        for messages in provider.inputs
    )
    assert any(isinstance(m, ToolMessage) for m in provider.inputs[1])
    with service_session_factory() as db:
        public = list(db.scalars(select(Message).where(Message.session_id == sid)))
        assert len(public) == 2 and all(m.source_agent_message_id for m in public)
        stream = list(
            db.scalars(
                select(AgentMessage)
                .where(AgentMessage.session_id == sid)
                .order_by(AgentMessage.stream_seq)
            )
        )
        assert [r.kind for r in stream].count("BUYER_INPUT") == 1
        assert [r.kind for r in stream].count("TOOL_RESULT") == 2
        task = db.scalar(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        assert task.runtime_kind == "V4" and task.runtime_checkpoint["calls"] == 2
        assert (
            len(
                list(
                    db.scalars(
                        select(AgentCallSnapshot).where(AgentCallSnapshot.model_task_id == task.id)
                    )
                )
            )
            == 2
        )


@pytest.mark.parametrize("terms", [None, [], "invalid"])
def test_rejected_terms_are_recorded_and_loop_recovers(service_session_factory, v4, terms):
    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(
        call(
            "evaluate_offer",
            {"price": "2700", "shipping_paid_by": "buyer", "additional_terms": terms},
        ),
        call("finish_turn", finish(), "finish"),
    )
    result = ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="rejected-terms", content="介绍商品"
    )
    assert result.outcome == "INFORMATIONAL"
    with service_session_factory() as db:
        task = db.scalar(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        assert task.status.value == "SUCCEEDED"
        receipts = list(
            db.scalars(
                select(AgentMessage).where(
                    AgentMessage.session_id == sid, AgentMessage.kind == "TOOL_RESULT"
                )
            )
        )
        assert len(receipts) == 2
        assert json.loads(receipts[0].payload["message"]["data"]["content"])["ok"] is False
        memory = db.get(AgentSessionContext, sid).memory_json
        assert json.loads(memory["entries"][0]["value"])["ok"] is False


def test_tool_memory_is_in_next_persisted_request(service_session_factory, v4):
    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(
        call("evaluate_offer", {"price": "2700", "shipping_paid_by": "buyer"}),
        call("finish_turn", finish(), "finish"),
    )
    ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="current-memory", content="谈谈交易条件"
    )
    with service_session_factory() as db:
        task = db.scalar(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        snapshot = db.scalar(
            select(AgentCallSnapshot)
            .where(AgentCallSnapshot.model_task_id == task.id)
            .order_by(AgentCallSnapshot.id.desc())
        )
        facts = json.loads(
            snapshot.request_payload["provider_messages"][1]["content"].split("\n", 1)[1]
        )
        assert facts["memory"] == db.get(AgentSessionContext, sid).memory_json
        assert facts["memory"]["entries"][0]["kind"] == "ATTEMPTED_OPTION"


def test_oversized_tool_id_never_reaches_database_identifiers(service_session_factory, v4):
    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(
        call("get_product_info", call_id="x" * 193), call("finish_turn", finish(), "finish")
    )
    result = ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="long-tool-id", content="介绍商品"
    )
    assert result.outcome == "INFORMATIONAL" and len(provider.inputs) == 2
    with service_session_factory() as db:
        assert not list(
            db.scalars(
                select(AgentMessage).where(
                    AgentMessage.session_id == sid, AgentMessage.tool_call_id == "x" * 193
                )
            )
        )


def test_history_query_keeps_sources_without_recursive_artifacts(service_session_factory, v4):
    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(
        call("search_conversation_history", {"query": "", "limit": 12}, "search-1"),
        call("search_conversation_history", {"query": "", "limit": 12}, "search-2"),
        call("finish_turn", finish(), "finish"),
    )
    result = ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="history-query", content="介绍商品"
    )
    assert result.outcome == "INFORMATIONAL"
    with service_session_factory() as db:
        rows = list(
            db.scalars(
                select(AgentMessage)
                .where(AgentMessage.session_id == sid, AgentMessage.tool_call_id == "search-2")
                .order_by(AgentMessage.stream_seq)
            )
        )
        history = rows[0].payload["message"]["data"]["artifact"]["full_result"]
        assert history["records"][0]["kind"] == "BUYER_INPUT"
        assert all("artifact" not in r["payload"]["message"] for r in history["records"])
        assert all(r["kind"] != "RUN_CONFIG" for r in history["records"])


def test_v4_emits_each_actual_call_and_completed_turn_once(service_session_factory, v4):
    from app.observability.service import ObservabilityService

    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(call("get_product_info"), call("finish_turn", finish(), "finish"))
    chat = ChatService(service_session_factory, provider)
    for _ in range(2):
        chat.send_buyer_message(
            session_id=sid, buyer_id=buyer, request_id="observed-v4", content="介绍商品"
        )
    events = ObservabilityService(service_session_factory).list_events(session_id=sid)
    actual = [e for e in events if e.event_type == "MODEL_CALL_COMPLETED"]
    assert len(actual) == 2 and all(e.attributes["model_call_purpose"] == "REACT" for e in actual)
    assert sum(e.input_tokens for e in actual) == 200
    assert sum(e.event_type == "CHAT_TURN_COMPLETED" for e in events) == 1


def test_v4_rpc_failure_is_observed_without_a_duplicate_call_event(service_session_factory, v4):
    from app.observability.service import ObservabilityService

    sid, buyer = create_negotiation(service_session_factory)
    with pytest.raises(ModelDecisionError):
        ChatService(
            service_session_factory, ScriptProvider(TimeoutError("temporary"))
        ).send_buyer_message(
            session_id=sid, buyer_id=buyer, request_id="observed-error", content="介绍商品"
        )
    events = ObservabilityService(service_session_factory).list_events(session_id=sid)
    actual = [e for e in events if e.event_type == "MODEL_CALL_COMPLETED"]
    assert len(actual) == 1 and actual[0].outcome == "ERROR" and actual[0].input_tokens is None
    assert any(e.event_type == "MODEL_TASK_ATTEMPT_FAILED" for e in events)


def test_free_text_gets_bounded_named_finish_with_actual_snapshot(service_session_factory, v4):
    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(AIMessage(content="这里是商品介绍"), call("finish_turn", finish()))
    result = ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="named-finish", content="介绍商品"
    )
    assert result.outcome == "INFORMATIONAL"
    assert provider.purposes == ["REACT", "REACT_ENDING"]
    assert [t["function"]["name"] for t in provider.tool_inputs[-1]] == ["finish_turn"]
    with service_session_factory() as db:
        task = db.scalar(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        ending = db.scalar(
            select(AgentCallSnapshot).where(
                AgentCallSnapshot.model_task_id == task.id,
                AgentCallSnapshot.purpose == "REACT_ENDING",
            )
        )
        assert ending.logical_step_id == "REACT_ENDING:2"
        assert ending.request_payload["tool_choice"] == {
            "type": "function",
            "function": {"name": "finish_turn"},
        }
        assert task.runtime_checkpoint["calls"] == 2


@pytest.mark.parametrize("interrupt_before_step", [False, True])
def test_json_finish_is_observed_and_recovered_without_another_call(
    service_session_factory, v4, monkeypatch, interrupt_before_step
):
    from app.services.agent_runtime_service import ProductionRuntimeBackend

    sid, buyer = create_negotiation(service_session_factory)
    response = AIMessage(
        content=json.dumps(finish(), ensure_ascii=False),
        usage_metadata={"input_tokens": 120, "output_tokens": 50, "total_tokens": 170},
    )
    provider = ScriptProvider(
        AIMessage(content="介绍商品"), AIMessage(content="仍返回文字"), response
    )
    original = ProductionRuntimeBackend.save_step
    interrupted = False

    def save_step(self, kind, payload, checkpoint):
        nonlocal interrupted
        if interrupt_before_step and not interrupted and payload.get("tool_call_origin"):
            interrupted = True
            raise TimeoutError("interrupted after JSON response snapshot")
        return original(self, kind, payload, checkpoint)

    monkeypatch.setattr(ProductionRuntimeBackend, "save_step", save_step)
    chat = ChatService(service_session_factory, provider)
    request = dict(session_id=sid, buyer_id=buyer, request_id="json-finish", content="介绍商品")
    if interrupt_before_step:
        with pytest.raises(ModelDecisionError):
            chat.send_buyer_message(**request)
        ready_retry(service_session_factory, sid)
    result = chat.send_buyer_message(**request)
    assert result.outcome == "INFORMATIONAL"
    assert provider.purposes == ["REACT", "REACT_ENDING", "REACT_FINISH"]
    assert provider.tool_inputs[-1] == []
    with service_session_factory() as db:
        task = db.scalar(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        assert task.input_snapshot["agent_config"]["ending_strategy"] == "structured-finish-v2"
        assert task.runtime_checkpoint["calls"] == task.runtime_checkpoint["steps"] == 3
        snapshots = list(
            db.scalars(select(AgentCallSnapshot).where(AgentCallSnapshot.model_task_id == task.id))
        )
        assert len(snapshots) == 3
        snapshot = next(s for s in snapshots if s.purpose == "REACT_FINISH")
        assert snapshot.logical_step_id == "REACT_FINISH:3"
        assert snapshot.request_payload["response_format"]["type"] == "json_schema"
        assert snapshot.request_payload["tools"] == []
        assert snapshot.response_payload["data"]["content"] == response.content
        stream = list(
            db.scalars(
                select(AgentMessage)
                .where(AgentMessage.session_id == sid)
                .order_by(AgentMessage.stream_seq)
            )
        )
        internal = [r for r in stream if r.payload.get("tool_call_origin") == "STRUCTURED_FINISH"]
        assert len(internal) == 1
        assert internal[0].payload["model_response"]["data"]["content"] == response.content
        assert len([r for r in stream if r.kind == "TOOL_RESULT"]) == 1
        assert len(list(db.scalars(select(Message).where(Message.session_id == sid)))) == 2


def test_rejected_write_feedback_can_lead_to_authorized_counter(service_session_factory, v4):
    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(
        call("submit_counter_offer", {"price": "2750.00", "shipping_paid_by": "buyer"}),
        call("submit_counter_offer", {"price": "2900.00", "shipping_paid_by": "buyer"}, "call-2"),
    )
    turn = ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="v4-counter", content="给个可以接受的报价"
    )
    assert turn.outcome == "COUNTER_OFFERED"
    assert "2900.00" in turn.agent_message.content
    assert '"ok": false' in next(
        m.content for m in provider.inputs[1] if isinstance(m, ToolMessage)
    )
    replay = ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="v4-counter", content="给个可以接受的报价"
    )
    assert replay.idempotent_replay
    assert replay.formal_offer_id == turn.formal_offer_id


def test_memory_is_source_checked_and_conditions_are_replaced(service_session_factory, v4):
    sid, buyer = create_negotiation(service_session_factory)
    first = [{"kind": "BUYER_STATEMENT", "field": "delivery", "value": "自提", "source_seqs": [1]}]
    provider = ScriptProvider(call("finish_turn", finish(first)))
    chat = ChatService(service_session_factory, provider)
    chat.send_buyer_message(session_id=sid, buyer_id=buyer, request_id="mem-1", content="我能自提")
    with service_session_factory() as db:
        context = db.get(AgentSessionContext, sid)
        assert context.memory_json["entries"][0]["value"] == "自提"
        next_seq = context.next_stream_seq
    provider.responses.append(
        call(
            "finish_turn",
            finish(
                [
                    {
                        "kind": "BUYER_STATEMENT",
                        "field": "delivery",
                        "value": "快递",
                        "source_seqs": [next_seq],
                    }
                ]
            ),
            "call-2",
        )
    )
    chat.send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="mem-2", content="改成快递吧"
    )
    with service_session_factory() as db:
        entries = db.get(AgentSessionContext, sid).memory_json["entries"]
        assert entries[0]["status"] == "REPLACED"
        assert entries[-1]["value"] == "快递"


def test_recovery_reuses_completed_tool_results(service_session_factory, v4):
    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(
        call("get_product_info"),
        TimeoutError("temporary"),
        call("finish_turn", finish(), "call-2"),
    )
    chat = ChatService(service_session_factory, provider)
    with pytest.raises(ModelDecisionError):
        chat.send_buyer_message(
            session_id=sid, buyer_id=buyer, request_id="recover", content="介绍商品"
        )
    with service_session_factory() as db, db.begin():
        task = db.scalar(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        task.next_retry_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    turn = chat.send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="recover", content="介绍商品"
    )
    assert turn.outcome == "INFORMATIONAL"
    assert any(isinstance(m, ToolMessage) for m in provider.inputs[-1])
    with service_session_factory() as db:
        results = list(
            db.scalars(
                select(AgentMessage).where(
                    AgentMessage.session_id == sid, AgentMessage.kind == "TOOL_RESULT"
                )
            )
        )
        assert len(results) == 2


def ready_retry(factory, sid):
    with factory() as db, db.begin():
        task = db.scalar(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        task.next_retry_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)


@pytest.mark.parametrize("crash_on_ending", [False, True])
def test_crash_after_model_response_reuses_call_snapshot(
    service_session_factory, v4, monkeypatch, crash_on_ending
):
    from app.services.agent_runtime_service import ProductionRuntimeBackend

    sid, buyer = create_negotiation(service_session_factory)
    first = AIMessage(content="先介绍商品") if crash_on_ending else call("get_product_info")
    provider = ScriptProvider(first, call("finish_turn", finish(), "call-2"))
    original = ProductionRuntimeBackend.save_step
    interrupted = False

    def fail_once(self, kind, payload, checkpoint):
        nonlocal interrupted
        if not interrupted and (not crash_on_ending or checkpoint["steps"] == 2):
            interrupted = True
            raise TimeoutError("process interrupted before ledger checkpoint")
        return original(self, kind, payload, checkpoint)

    monkeypatch.setattr(ProductionRuntimeBackend, "save_step", fail_once)
    chat = ChatService(service_session_factory, provider)
    with pytest.raises(ModelDecisionError):
        chat.send_buyer_message(
            session_id=sid, buyer_id=buyer, request_id="response-crash", content="商品详情"
        )
    ready_retry(service_session_factory, sid)
    result = chat.send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="response-crash", content="商品详情"
    )
    assert result.outcome == "INFORMATIONAL" and len(provider.inputs) == 2
    with service_session_factory() as db:
        task = db.scalar(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        assert task.runtime_checkpoint["calls"] == 2
        assert (
            len(
                list(
                    db.scalars(
                        select(AgentCallSnapshot).where(AgentCallSnapshot.model_task_id == task.id)
                    )
                )
            )
            == 2
        )


def test_named_ending_cannot_execute_another_business_tool(service_session_factory, v4):
    from app.db.models import Offer

    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(
        AIMessage(content="想谈一个价格"),
        call("submit_counter_offer", {"price": "2900", "shipping_paid_by": "buyer"}),
        call("finish_turn", finish(), "finish-only"),
    )
    result = ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="ending-no-write", content="介绍商品"
    )
    assert result.outcome == "INFORMATIONAL"
    assert provider.purposes == ["REACT", "REACT_ENDING", "REACT_ENDING"]
    with service_session_factory() as db:
        assert not list(db.scalars(select(Offer).where(Offer.session_id == sid)))


def test_invalid_multi_terminal_batch_is_fully_closed_without_write(service_session_factory, v4):
    from app.db.models import Offer

    sid, buyer = create_negotiation(service_session_factory)
    invalid = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "a",
                "name": "submit_counter_offer",
                "args": {"price": "2900", "shipping_paid_by": "buyer"},
            },
            {"id": "b", "name": "finish_turn", "args": finish()},
        ],
    )
    provider = ScriptProvider(invalid, call("finish_turn", finish(), "c"))
    ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="invalid-batch", content="商品信息"
    )
    assert len([m for m in provider.inputs[-1] if isinstance(m, ToolMessage)]) == 2
    with service_session_factory() as db:
        assert not list(db.scalars(select(Offer).where(Offer.session_id == sid)))


def test_version_change_blocks_pending_write(service_session_factory, v4):
    from app.db.models import NegotiationSession, Offer

    sid, buyer = create_negotiation(service_session_factory)

    class ChangingProvider(ScriptProvider):
        def invoke_agent(self, *args, **kwargs):
            with service_session_factory() as db, db.begin():
                db.get(NegotiationSession, sid).version += 1
            return super().invoke_agent(*args, **kwargs)

    provider = ChangingProvider(
        call("submit_counter_offer", {"price": "2900", "shipping_paid_by": "buyer"})
    )
    result = ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="changed", content="谈谈价格"
    )
    assert result.outcome == "SAFE_FAILURE"
    with service_session_factory() as db:
        assert not list(db.scalars(select(Offer).where(Offer.session_id == sid)))
        task = db.scalar(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        assert task.status.value == "STALE"
        assert task.runtime_checkpoint["pending_calls"] == []


def test_old_lease_cannot_append_response_or_write(service_session_factory, v4):
    from app.db.models import Offer
    from app.services.errors import IncompleteRequestError

    sid, buyer = create_negotiation(service_session_factory)

    class StealingProvider(ScriptProvider):
        def invoke_agent(self, *args, **kwargs):
            with service_session_factory() as db, db.begin():
                task = db.scalar(
                    select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid)
                )
                task.lease_token = "new-owner"
            return super().invoke_agent(*args, **kwargs)

    provider = StealingProvider(
        call("submit_counter_offer", {"price": "2900", "shipping_paid_by": "buyer"})
    )
    with pytest.raises(IncompleteRequestError):
        ChatService(service_session_factory, provider).send_buyer_message(
            session_id=sid, buyer_id=buyer, request_id="lease", content="谈谈价格"
        )
    with service_session_factory() as db:
        assert not list(db.scalars(select(Offer).where(Offer.session_id == sid)))
        assert not list(
            db.scalars(
                select(AgentMessage).where(
                    AgentMessage.session_id == sid, AgentMessage.kind == "TOOL_CALL"
                )
            )
        )


def test_formal_write_and_receipt_rollback_together(service_session_factory, v4, monkeypatch):
    from app.db.models import Offer
    from app.services.agent_runtime_service import ProductionRuntimeBackend

    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(
        call("submit_counter_offer", {"price": "2900", "shipping_paid_by": "buyer"})
    )
    original = ProductionRuntimeBackend._tool_result
    failed = False

    def interrupted(self, db, invocation, value, checkpoint):
        nonlocal failed
        if not failed:
            failed = True
            raise TimeoutError("crash before receipt")
        return original(self, db, invocation, value, checkpoint)

    monkeypatch.setattr(ProductionRuntimeBackend, "_tool_result", interrupted)
    chat = ChatService(service_session_factory, provider)
    with pytest.raises(ModelDecisionError):
        chat.send_buyer_message(
            session_id=sid, buyer_id=buyer, request_id="write-crash", content="给个正式价格"
        )
    with service_session_factory() as db:
        assert not list(db.scalars(select(Offer).where(Offer.session_id == sid)))
    ready_retry(service_session_factory, sid)
    result = chat.send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="write-crash", content="给个正式价格"
    )
    assert result.outcome == "COUNTER_OFFERED" and len(provider.inputs) == 1
    with service_session_factory() as db:
        assert len(list(db.scalars(select(Offer).where(Offer.session_id == sid)))) == 1


def test_budget_exhaustion_closes_protocol(service_session_factory, v4, monkeypatch):
    monkeypatch.setattr(
        "app.services.chat_service.get_settings",
        lambda: v4.model_copy(update={"agent_max_tool_calls": 1}),
    )
    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(call("get_product_info"), call("finish_turn", finish(), "call-2"))
    result = ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="budget", content="商品介绍"
    )
    assert result.outcome == "SAFE_FAILURE"
    with service_session_factory() as db:
        task = db.scalar(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        assert task.runtime_checkpoint["stop_reason"] == "TOOL_BUDGET"
        assert task.runtime_checkpoint["pending_calls"] == []


def test_bad_memory_does_not_undo_reply_and_next_turn_repairs(service_session_factory, v4):
    sid, buyer = create_negotiation(service_session_factory)
    bad = [{"kind": "BUYER_STATEMENT", "field": "delivery", "value": "快递", "source_seqs": [1]}]
    provider = ScriptProvider(
        call("finish_turn", finish(bad)),
        AIMessage(
            content='{"entries":[{"kind":"BUYER_STATEMENT","field":"delivery","value":"自提","source_seqs":[1]}]}'
        ),
        call("finish_turn", finish(), "call-2"),
    )
    chat = ChatService(service_session_factory, provider)
    assert (
        chat.send_buyer_message(
            session_id=sid, buyer_id=buyer, request_id="bad-mem", content="我能自提"
        ).outcome
        == "INFORMATIONAL"
    )
    assert (
        chat.send_buyer_message(
            session_id=sid, buyer_id=buyer, request_id="repair-mem", content="介绍商品"
        ).outcome
        == "INFORMATIONAL"
    )
    with service_session_factory() as db:
        memory = db.get(AgentSessionContext, sid).memory_json
        assert memory["entries"][0]["value"] == "自提"
        calls = list(
            db.scalars(
                select(AgentCallSnapshot)
                .join(ModelExecutionTask)
                .where(ModelExecutionTask.session_id == sid)
            )
        )
        assert any(c.purpose == "MEMORY" for c in calls)


def test_failed_catchup_is_not_skipped_by_successful_current_memory(service_session_factory, v4):
    sid, buyer = create_negotiation(service_session_factory)
    bad = [{"kind": "BUYER_STATEMENT", "field": "delivery", "value": "快递", "source_seqs": [1]}]
    provider = ScriptProvider(
        call("finish_turn", finish(bad)),
        AIMessage(content="invalid memory JSON"),
        call("finish_turn", finish(), "finish-second"),
        AIMessage(
            content=(
                '{"entries":[{"kind":"BUYER_STATEMENT","field":"delivery",'
                '"value":"自提","source_seqs":[1]}]}'
            )
        ),
        call("finish_turn", finish(), "finish-third"),
    )
    chat = ChatService(service_session_factory, provider)
    for request_id, content in (("gap-first", "我能自提"), ("gap-second", "介绍商品")):
        assert (
            chat.send_buyer_message(
                session_id=sid, buyer_id=buyer, request_id=request_id, content=content
            ).outcome
            == "INFORMATIONAL"
        )
    with service_session_factory() as db:
        assert db.get(AgentSessionContext, sid).memory_covered_through_seq == 0
    assert (
        chat.send_buyer_message(
            session_id=sid, buyer_id=buyer, request_id="gap-third", content="继续介绍"
        ).outcome
        == "INFORMATIONAL"
    )
    with service_session_factory() as db:
        context = db.get(AgentSessionContext, sid)
        assert context.memory_covered_through_seq > 1
        assert context.memory_json["entries"][0]["value"] == "自提"
    assert provider.purposes.count("MEMORY") == 2


def test_evaluation_adapter_matches_mysql_tool_contracts(service_session_factory):
    from decimal import Decimal

    from app.agent.tools import AgentToolContext, build_seller_tools
    from app.services.approval_service import ApprovalService
    from app.services.negotiation_service import NegotiationService
    from app.services.pricing_service import OfferTerms, ShippingPayer
    from app.services.product_service import ProductService
    from evaluation.environment import EvaluationEnvironment
    from evaluation.scenarios import load_scenarios
    from evaluation.schemas import OfferSpec

    sid, buyer = create_negotiation(service_session_factory)
    env = EvaluationEnvironment(load_scenarios().scenarios[0])

    def actual(offer_id=None):
        return {
            t.name: t
            for t in build_seller_tools(
                context=AgentToolContext(sid, buyer, offer_id),
                product_service=ProductService(service_session_factory),
                negotiation_service=NegotiationService(service_session_factory),
                approval_service=ApprovalService(service_session_factory),
            )
        }

    for price in ("2600", "2700", "2800", "2850", "2900"):
        args = {"price": price, "shipping_paid_by": "buyer"}
        assert actual()["evaluate_offer"].invoke(args) == env.evaluate(**args)
    args = {"price": "2900", "shipping_paid_by": "buyer"}
    stored = actual()["submit_counter_offer"].invoke(args)["offer"]
    simulated = env.counter(**args)["offer"]
    for key in ("proposer", "status", "shipping_paid_by", "additional_terms", "expires_at"):
        assert stored[key] == simulated[key]
    assert Decimal(stored["price"]) == Decimal(simulated["price"])
    assert (
        actual()["get_negotiation_state"].invoke({})["negotiation"]["round_count"]
        == env.round_count
        == 0
    )
    buyer_offer = NegotiationService(service_session_factory).record_buyer_offer(
        session_id=sid,
        buyer_id=buyer,
        terms=OfferTerms(Decimal("2800"), ShippingPayer.BUYER, None, Decimal("0")),
    )
    env.buyer_offer(OfferSpec(price="2800", shipping_paid_by="buyer"))
    assert (
        actual()["get_negotiation_state"].invoke({})["negotiation"]["recent_offers"][0]["status"]
        == env.state()["negotiation"]["recent_offers"][0]["status"]
        == "REJECTED"
    )
    real = actual(buyer_offer.id)["request_approval"].invoke(
        {"offer_id": buyer_offer.id, "reason": "需要确认"}
    )
    simulated = env.request_approval(env.current_offer_id, "需要确认")
    assert real["ok"] == simulated["ok"] is True
    assert real["approval"]["status"] == simulated["approval"]["status"] == "PENDING"
    assert (
        actual()["get_negotiation_state"].invoke({})["negotiation"]["status"]
        == env.status
        == "WAITING_APPROVAL"
    )


def test_manual_termination_projects_reply_and_closes_pending_tools(
    service_session_factory, v4, monkeypatch
):
    from app.db.models import NegotiationSession, Product
    from app.services.agent_runtime_service import ProductionRuntimeBackend
    from app.services.model_task_recovery_service import ModelTaskRecoveryService

    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(call("get_product_info"))

    def interrupted(*args, **kwargs):
        raise TimeoutError("interrupted before tool")

    monkeypatch.setattr(ProductionRuntimeBackend, "execute", interrupted)
    chat = ChatService(service_session_factory, provider)
    with pytest.raises(ModelDecisionError):
        chat.send_buyer_message(
            session_id=sid, buyer_id=buyer, request_id="manual-stop", content="商品介绍"
        )
    with service_session_factory() as db:
        task = db.scalar(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        task_id = task.id
        seller = db.get(Product, db.get(NegotiationSession, sid).product_id).seller_id
    ModelTaskRecoveryService(service_session_factory).terminate(
        seller_id=seller, task_id=task_id, reason="停止测试"
    )
    with service_session_factory() as db:
        task = db.get(ModelExecutionTask, task_id)
        assert task.runtime_checkpoint["pending_calls"] == []
        assert all(
            m.source_agent_message_id
            for m in db.scalars(select(Message).where(Message.session_id == sid))
        )
        assert db.scalar(
            select(AgentMessage).where(
                AgentMessage.model_task_id == task_id, AgentMessage.kind == "TOOL_RESULT"
            )
        )
        from app.agent.message_protocol import StreamRecord, record_message

        rows = list(
            db.scalars(
                select(AgentMessage)
                .where(AgentMessage.session_id == sid)
                .order_by(AgentMessage.stream_seq)
            )
        )
        MessageProtocolValidator.validate(
            [
                message
                for row in rows
                if (message := record_message(StreamRecord.from_row(row))) is not None
            ]
        )
