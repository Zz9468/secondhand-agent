import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.semantic.contracts import IntentFrame
from app.semantic.model import PROMPT_VERSION, SemanticModel
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


def test_sdk_has_no_implicit_retries_or_private_context(monkeypatch):
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
        {"model_name": "qwen3.7-flash", "prompt_version": PROMPT_VERSION},
    )
    assert frame["intents"][0]["candidate"]["amount"] == "900"
    assert tokens == 150 and cost == Decimal("0.00006")
    assert captured["client"]["max_retries"] == 0
    assert captured["request"]["max_tokens"] == 2048
    assert captured["request"]["extra_body"] == {"enable_thinking": False}
    assert "synthetic-not-real" not in str(captured["request"])


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
