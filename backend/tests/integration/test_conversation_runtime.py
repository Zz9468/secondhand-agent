import json

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.db.models import AgentCallSnapshot, AgentMessage, Message, ModelExecutionTask, Offer
from app.services.chat_service import ChatService
from app.services.errors import ModelDecisionError
from tests.integration.factories import create_negotiation
from tests.integration.test_v4_runtime import ScriptProvider, call, ready_retry

pytestmark = pytest.mark.mysql_integration


@pytest.fixture
def conversation(monkeypatch):
    from app.core.config import get_settings

    settings = get_settings().model_copy(
        update={
            "agent_runtime": "V4",
            "agent_v4_session_ids": None,
            "agent_reply_strategy": "conversation-v1",
            "agent_reply_review_enable_thinking": True,
        }
    )
    monkeypatch.setattr("app.services.chat_service.get_settings", lambda: settings)


def plan(reply="可以讨论价格，具体能否调整需要评估后确认。"):
    return {"decision": {"action": "INQUIRY", "reason": "回应买家", "reply": reply, "topics": []}}


def review(approved=True):
    return AIMessage(
        content=json.dumps({"approved": approved, "reason": "事实审核结果", "fact_checks": []}),
        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    )


def test_open_reply_persists_reviews_and_idempotent_public_projection(
    service_session_factory,
    conversation,
):
    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(call("finish_turn", plan()), review())
    chat = ChatService(service_session_factory, provider)
    request = dict(session_id=sid, buyer_id=buyer, request_id="open-chat", content="支持讲价吗？")
    first = chat.send_buyer_message(**request)
    replay = chat.send_buyer_message(**request)
    assert first.agent_message.content == plan()["decision"]["reply"]
    assert first.outcome == "INFORMATIONAL" and replay.idempotent_replay
    assert provider.purposes == ["REACT", "REPLY_REVIEW"]
    with service_session_factory() as db:
        task = db.scalar(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        assert task.input_snapshot["agent_config"]["reply_strategy"] == "conversation-v1"
        assert task.runtime_checkpoint["calls"] == 2
        snapshots = list(
            db.scalars(select(AgentCallSnapshot).where(AgentCallSnapshot.model_task_id == task.id))
        )
        assert len(snapshots) == 2
        audit = next(s for s in snapshots if s.purpose == "REPLY_REVIEW")
        assert audit.request_payload["response_format"]["json_schema"]["name"] == "ReplyReview"
        assert audit.request_payload["model"]["thinking"] is True
        assert audit.request_payload["max_output_tokens"] == 3072
        assert len(audit.logical_step_id) == 64
        assert audit.logical_step_id == audit.context_manifest["reply_review_key"]
        assert len(list(db.scalars(select(Message).where(Message.session_id == sid)))) == 2
        assert db.scalar(select(Offer).where(Offer.session_id == sid)) is None
        result = db.scalar(
            select(AgentMessage).where(
                AgentMessage.session_id == sid, AgentMessage.kind == "TOOL_RESULT"
            )
        )
        assert result.payload["message"]["data"]["artifact"]["full_result"]["reply_reviewed"]


@pytest.mark.parametrize("after_verdict", [False, True])
def test_review_recovery_reuses_snapshot_or_verdict_without_another_rpc(
    service_session_factory,
    conversation,
    monkeypatch,
    after_verdict,
):
    from app.services.agent_runtime_service import ProductionRuntimeBackend

    sid, buyer = create_negotiation(service_session_factory)
    provider = ScriptProvider(call("finish_turn", plan()), review())
    original = ProductionRuntimeBackend.save_checkpoint
    interrupted = False

    def interrupt(self, cp):
        nonlocal interrupted
        is_reconciled = any(
            k.startswith("REPLY_REVIEW:") and v["reconciled"]
            for k, v in cp.get("call_reservations", {}).items()
        )
        if not interrupted and (bool(cp.get("reply_reviews")) if after_verdict else is_reconciled):
            interrupted = True
            raise TimeoutError("interrupted after review snapshot or verdict")
        return original(self, cp)

    monkeypatch.setattr(ProductionRuntimeBackend, "save_checkpoint", interrupt)
    chat = ChatService(service_session_factory, provider)
    request = dict(
        session_id=sid, buyer_id=buyer, request_id="review-recover", content="能讲价吗？"
    )
    with pytest.raises(ModelDecisionError):
        chat.send_buyer_message(**request)
    ready_retry(service_session_factory, sid)
    result = chat.send_buyer_message(**request)
    assert result.outcome == "INFORMATIONAL"
    assert provider.purposes == ["REACT", "REPLY_REVIEW"]
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


def test_reply_review_repair_is_recorded_but_never_public(service_session_factory, conversation):
    sid, buyer = create_negotiation(service_session_factory)
    corrected = "商品记录没有保修期限，需要卖家进一步确认。"
    provider = ScriptProvider(
        call("finish_turn", plan("还有一年保修。")),
        review(False),
        call("finish_turn", plan(corrected), "repair"),
        review(),
    )
    result = ChatService(service_session_factory, provider).send_buyer_message(
        session_id=sid, buyer_id=buyer, request_id="review-repair", content="还有多久保修？"
    )
    assert result.agent_message.content == corrected
    with service_session_factory() as db:
        public = list(db.scalars(select(Message).where(Message.session_id == sid)))
        assert len(public) == 2 and "还有一年保修" not in public[-1].content
        task = db.scalar(select(ModelExecutionTask).where(ModelExecutionTask.session_id == sid))
        assert task.runtime_checkpoint["calls"] == 4
