import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.semantic.contracts import IntentFrame
from app.semantic.model import (
    LEGACY_PROMPT,
    PROMPT_VERSION,
    SemanticModel,
    SemanticResponseError,
    model_messages,
)
from app.semantic.service import safe_frame


def settings(**kwargs):
    return Settings(
        _env_file=None,
        database_url="mysql+pymysql://test:test@localhost/test",
        model_name="qwen3.7-flash",
        model_api_key="synthetic-not-real",
        model_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        **kwargs,
    )


def test_known_endpoint_cost_bound_and_unknown_price_refusal():
    model = SemanticModel(settings())
    assert model.prices() == (Decimal("0.2"), Decimal("0.8"))
    tokens, cost = model.reserve("900自提", {})
    assert 2048 < tokens < 32768 and Decimal("0") < cost < Decimal("0.1")
    unknown = SemanticModel(
        model.settings.model_copy(update={"model_base_url": "https://example.invalid"})
    )
    assert unknown.reserve("900自提", {})[1] is None
    workspace = SemanticModel(
        model.settings.model_copy(
            update={
                "model_base_url": "https://ws-synthetic.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
            }
        )
    )
    assert workspace.prices() == model.prices()


@pytest.mark.parametrize(
    "prompt_version",
    [
        "p2-intents-v1",
        "p2-intents-v2",
        "p2-intents-v3",
        "p2-intents-v4",
        "p2-intents-v5",
        "p2-intents-v6",
        "p2-intents-v7",
        "p2-intents-v8",
        PROMPT_VERSION,
    ],
)
def test_sdk_has_no_implicit_retries_or_private_context(monkeypatch, prompt_version):
    captured = {}
    value = {
        "intents": [
            {
                "id": "i",
                "type": "PROPOSE_OFFER",
                "source_quote": "900自提",
                "candidate": {"amount": "900", "delivery": "pickup", "payer": "buyer"},
            }
        ]
    }

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = SimpleNamespace(completions=self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, **kwargs):
            captured["request"] = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop", message=SimpleNamespace(content=json.dumps(value))
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            )

    monkeypatch.setattr("app.semantic.model.OpenAI", Client)
    model = SemanticModel(settings())
    frame, tokens, cost = model(
        "900自提",
        {"has_bound_draft": False},
        {"model_name": "qwen3.7-flash", "prompt_version": prompt_version},
    )
    assert frame["intents"][0]["candidate"]["amount"] == "900"
    assert tokens == 150 and cost == Decimal("0.00006")
    assert captured["client"]["max_retries"] == 0
    assert captured["request"]["max_tokens"] == 2048
    assert captured["request"]["extra_body"] == {"enable_thinking": False}
    response_format = captured["request"]["response_format"]
    assert response_format["type"] == (
        "json_schema"
        if prompt_version in {"p2-intents-v7", "p2-intents-v8", PROMPT_VERSION}
        else "json_object"
    )
    if prompt_version == PROMPT_VERSION:
        assert response_format["json_schema"]["strict"] is True
        assert response_format["json_schema"]["schema"]["$defs"]["Intent"]["allOf"]
    assert "synthetic-not-real" not in str(captured["request"])
    value["intents"][0]["source_quote"] = "not in source"
    with pytest.raises(SemanticResponseError) as rejected:
        model("900自提", {}, {"model_name": "qwen3.7-flash", "prompt_version": prompt_version})
    assert rejected.value.tokens == 150 and rejected.value.cost == Decimal("0.00006")
    assert "not in source" not in str(rejected.value)


@pytest.mark.parametrize("text", ["我不接受900自提", "我不考虑900包邮报价"])
def test_explicit_negated_offer_is_not_authorized(text):
    frame = IntentFrame.model_validate(
        {
            "intents": [
                {
                    "id": "i",
                    "type": "PROPOSE_OFFER",
                    "source_quote": text,
                    "candidate": {"amount": "900"},
                }
            ]
        }
    )
    assert safe_frame(frame, text)[1] is False


def test_frozen_v1_prompt_remains_replayable():
    old = model_messages("900自提", {}, "p2-intents-v1")
    new = model_messages("900自提", {})
    assert old[0]["content"].startswith(LEGACY_PROMPT)
    assert "补充约束" not in old[0]["content"]
    assert "修改条款也必须提取candidate" in new[0]["content"]


@pytest.mark.parametrize("text", ["我不接受900自提", "900太贵了", "不要撤回报价", "不撤回"])
def test_withdrawal_requires_positive_explicit_instruction(text):
    frame = IntentFrame.model_validate(
        {"intents": [{"id": "i", "type": "WITHDRAW_OFFER", "source_quote": text}]}
    )
    assert safe_frame(frame, text)[1] is False


def test_negative_purchase_condition_applies_to_both_alternatives():
    text = "电池不到98%我就不买，900自提或者950包邮到上海。"
    predicate = {
        "fact": "battery_health_pct",
        "operator": ">=",
        "value": "98",
        "source_quote": "电池不到98%",
    }
    frame = IntentFrame.model_validate(
        {
            "intents": [
                {
                    "id": "a",
                    "type": "PROPOSE_OFFER",
                    "source_quote": "900自提",
                    "alternative_group": "choices",
                    "candidate": {
                        "amount": "900",
                        "delivery": "pickup",
                        "payer": "buyer",
                        "conditions": [predicate],
                    },
                },
                {
                    "id": "b",
                    "type": "PROPOSE_OFFER",
                    "source_quote": "950包邮到上海",
                    "alternative_group": "choices",
                    "candidate": {
                        "amount": "950",
                        "delivery": "shipping",
                        "payer": "seller",
                        "region": "上海",
                    },
                },
            ]
        }
    )
    assert safe_frame(frame, text)[1] is False
    frame.intents[1].candidate.conditions = frame.intents[0].candidate.conditions
    assert safe_frame(frame, text)[1] is True
