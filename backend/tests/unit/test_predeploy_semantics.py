"""D4: misclassified or truncated model quotes cannot grant confirmation authority."""

import pytest

from app.semantic.contracts import IntentFrame
from app.semantic.model import P4_PROMPT_VERSION, SemanticModel, parsing_contexts
from app.semantic.service import safe_frame
from tests.unit.test_semantic_model import settings


@pytest.mark.parametrize(
    ("text", "quote"),
    [
        ("我出800元，自提，现在提交这个报价。", "现在提交这个报价"),
        ("请提交我的报价：800元自提。", "请提交我的报价"),
        ("先别确认，我还要考虑。", "确认"),
        ("我不确认成交，只是问一下现在的报价。", "确认成交"),
        ("我不同意。", "同意"),
        ("不接受。", "接受"),
    ],
)
def test_d4_false_confirmation_is_vetoed_even_with_partial_quote(text, quote):
    frame = IntentFrame.model_validate(
        {
            "intents": [{"id": "c", "type": "CONFIRM_INTENT", "source_quote": quote}],
        }
    )
    assert safe_frame(frame, text)[1] is False


@pytest.mark.parametrize("version", ["p5-intents-v1", "p5-intents-v2", P4_PROMPT_VERSION])
def test_d4_frozen_parser_versions_retain_retry_and_context_budget(version):
    context = {"has_bound_draft": False, "has_displayed_offer": True}
    assert len(list(parsing_contexts(context, version))) == 2
    tokens, cost = SemanticModel(settings()).reserve(
        "请提交我的报价：800元自提。",
        context,
        prompt_version=version,
    )
    assert tokens < 32768 and cost is not None
