import json
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.decision_provider import DecisionRequest
from app.agent.model_observation import ObservedProviderResult, ProviderUsage
from app.core.config import Settings
from app.db.models import ModelExecutionTask, ObservabilityEvent
from app.observability.service import ObservabilityService
from app.services import chat_service as chat_service_module
from app.services.chat_service import BuyerOfferSubmission, ChatService
from app.services.errors import ModelDecisionError
from app.services.pricing_service import ShippingPayer
from tests.fakes import demo_negotiation_decision
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration


class MeasuredProvider:
    def decide_with_usage(
        self,
        request: DecisionRequest,
    ) -> ObservedProviderResult:
        return ObservedProviderResult(
            value=demo_negotiation_decision(request),
            usage=ProviderUsage(
                provider="qwen",
                model_name="qwen-plus",
                input_tokens=100,
                output_tokens=25,
                cached_input_tokens=20,
                total_tokens=125,
            ),
        )

    def decide(self, request: DecisionRequest):
        return self.decide_with_usage(request).value


class TimeoutProvider:
    def decide(self, request: DecisionRequest):
        raise TimeoutError("private upstream diagnostic must not be observed")


def test_chat_model_task_has_correlated_redacted_usage_and_cost_events(
    service_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url="mysql+pymysql://unused",
        model_input_price_per_million=Decimal("2"),
        model_output_price_per_million=Decimal("8"),
        model_cached_input_price_per_million=Decimal("0.5"),
        model_cost_currency="CNY",
    )
    monkeypatch.setattr(chat_service_module, "get_settings", lambda: settings)
    session_id, buyer_id = create_negotiation(service_session_factory)
    private_message = "这是不应进入观测属性的私密聊天内容"

    ChatService(service_session_factory, MeasuredProvider()).send_buyer_message(
        session_id=session_id,
        buyer_id=buyer_id,
        request_id="stage6-observation-001",
        content=private_message,
        offer=BuyerOfferSubmission(
            price=Decimal("2600.00"),
            shipping_paid_by=ShippingPayer.BUYER,
        ),
    )

    with service_session_factory() as db:
        task = db.scalar(
            select(ModelExecutionTask).where(
                ModelExecutionTask.session_id == session_id
            )
        )
        assert task is not None
        events = tuple(
            db.scalars(
                select(ObservabilityEvent)
                .where(ObservabilityEvent.session_id == session_id)
                .order_by(ObservabilityEvent.id)
            )
        )

    assert UUID(task.correlation_id)
    assert task.total_tokens == 125
    assert task.cached_input_tokens == 20
    assert task.estimated_cost == Decimal("0.00037000")
    assert task.cost_currency == "CNY"
    assert [event.event_type for event in events] == [
        "MODEL_TASK_ATTEMPT_STARTED",
        "MODEL_CALL_COMPLETED",
        "TOOL_ACTION_COMPLETED",
        "CHAT_TURN_COMPLETED",
    ]
    assert {event.correlation_id for event in events} == {task.correlation_id}
    model_event = events[1]
    assert model_event.total_tokens == 125
    assert model_event.input_price_per_million == Decimal("2.00000000")
    assert model_event.output_price_per_million == Decimal("8.00000000")
    assert model_event.cached_input_price_per_million == Decimal("0.50000000")
    assert model_event.estimated_cost == Decimal("0.00037000")
    serialized_attributes = json.dumps(
        [event.attributes for event in events],
        ensure_ascii=False,
    )
    assert private_message not in serialized_attributes
    assert buyer_id not in serialized_attributes


def test_failed_model_call_is_queryable_by_error_and_attempt(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    with pytest.raises(ModelDecisionError):
        ChatService(service_session_factory, TimeoutProvider()).send_buyer_message(
            session_id=session_id,
            buyer_id=buyer_id,
            request_id="stage6-timeout-001",
            content="触发受控超时",
        )

    with service_session_factory() as db:
        task = db.scalar(
            select(ModelExecutionTask).where(
                ModelExecutionTask.session_id == session_id
            )
        )
    assert task is not None
    events = ObservabilityService(service_session_factory).list_events(
        model_task_id=task.id,
        error_category="MODEL_TIMEOUT",
        min_attempt_count=1,
    )

    assert len(events) == 1
    assert events[0].event_type == "MODEL_CALL_COMPLETED"
    assert events[0].outcome == "ERROR"
    assert events[0].attributes["retry_scheduled"] is True
    assert "private upstream" not in json.dumps(events[0].to_json_dict())
