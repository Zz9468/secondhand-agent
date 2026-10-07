import json
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage

from app.agent.conversation_reply import (
    ConversationFinish,
    conversational,
    parse_finish,
    review_input,
    reviewed_reply,
)
from app.agent.decision_provider import response_format_contract
from app.agent.message_protocol import MessageProtocolValidator, record_message
from app.agent.runtime import SellerAgentRuntime
from app.agent.runtime_state import freeze_config
from app.core.config import get_settings
from evaluation.dynamic_buyer import BuyerAction
from evaluation.dynamic_runner import DynamicCallTracker
from evaluation.environment import EvaluationEnvironment
from evaluation.production_driver import ProductionDriver
from evaluation.scenarios import load_scenarios
from evaluation.schemas import OfferSpec


def finish(reply="可以讨论价格，具体是否调整需评估后确认。", topics=None, **extra):
    return {
        "decision": {
            "action": "INQUIRY",
            "reason": "回应当前问题",
            "reply": reply,
            "topics": topics or [],
            **extra,
        }
    }


def native(args, name="finish_turn", call_id="finish"):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}]
    )


def review(approved=True, reason="回复符合当前公开事实"):
    return AIMessage(
        content=json.dumps({"approved": approved, "reason": reason, "fact_checks": []})
    )


class Provider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.purposes = []
        self.inputs = []

    def invoke_agent(self, messages, tools, config, *, purpose):
        MessageProtocolValidator.validate(messages)
        self.purposes.append(purpose)
        self.inputs.append(deepcopy(messages))
        return self.responses.pop(0)


def driver(provider, *, thinking=False):
    config = freeze_config(
        get_settings().model_copy(
            update={
                "agent_reply_strategy": "conversation-v1",
                "model_enable_thinking": thinking,
                "agent_reply_review_enable_thinking": True,
            }
        )
    )
    config["budget"]["estimated_cost"] = None
    scenario = next(s for s in load_scenarios().scenarios if s.scenario_id == "inquiry_01")
    return ProductionDriver(
        EvaluationEnvironment(scenario), config, "G4", DynamicCallTracker(provider, config)
    )


@pytest.mark.parametrize("topics", [[], ["没有预定义过的自由主题"], ["dispatch", "price"]])
def test_topics_do_not_route_or_replace_reply(topics):
    provider = Provider(native(finish(topics=topics)), review())
    d = driver(provider)
    result = d.run_turn(BuyerAction(action="ASK", message="是否支持讲价？"))
    assert result.reply == finish()["decision"]["reply"]
    assert result.outcome.value == "INFORMATIONAL"
    assert provider.purposes == ["REACT", "REPLY_REVIEW"]
    recorded = d.call.calls[-1]
    assert recorded["reserved_tokens"] - recorded["estimated_input_tokens"] == 3072
    assert recorded["request"]["model"]["thinking"] is True
    assert not d.environment.offers and not d.environment.commitments
    assert d.last_backend.checkpoint["calls"] == 2


def test_unsupported_fact_is_repaired_and_only_reviewed_text_is_public():
    provider = Provider(
        native(finish("电池健康度100%，还有一年原厂保修。")),
        review(False, "没有这些电池和保修事实"),
        native(finish("商品记录尚未提供保修期限，需要向卖家确认。"), call_id="repair"),
        review(),
    )
    d = driver(provider)
    result = d.run_turn(BuyerAction(action="ASK", message="还有多久保修？"))
    assert "需要向卖家确认" in result.reply
    assert "100%" not in result.reply
    public = [r.payload["content"] for r in d.stream if r.kind == "PUBLIC_REPLY"]
    assert public == [result.reply]
    assert any("REPLY_REVIEW_REJECTED" in str(r.payload) for r in d.stream)
    assert "没有这些电池和保修事实" in provider.inputs[2][0].content
    assert not d.environment.commitments
    MessageProtocolValidator.validate([m for r in d.stream if (m := record_message(r))])


def test_affirmative_promise_is_blocked_before_review_and_negative_reply_can_pass():
    corrected = "目前无法保证今天发货，具体时间需要卖家确认。"
    provider = Provider(
        native(finish("我可以包邮，保证今天发货。")),
        native(finish(corrected), call_id="repair"),
        review(),
    )
    d = driver(provider)
    result = d.run_turn(BuyerAction(action="ASK", message="能包邮并保证今天发货吗？"))
    assert result.reply == corrected
    assert provider.purposes == ["REACT", "REACT_ENDING", "REPLY_REVIEW"]
    assert not d.environment.offers


@pytest.mark.parametrize("invalid", [AIMessage(content="not-json"), native({}, "get_product_info")])
def test_invalid_review_cannot_publish_candidate(invalid):
    repaired = finish("可以商量价格，最终条件需要评估和确认。")
    provider = Provider(native(finish()), invalid, native(repaired, call_id="repair"), review())
    d = driver(provider)
    result = d.run_turn(BuyerAction(action="ASK", message="能讲价吗？"))
    assert result.outcome.value == "INFORMATIONAL"
    assert len([r for r in d.stream if r.kind == "PUBLIC_REPLY"]) == 1
    assert len(d.last_backend.checkpoint["reply_reviews"]) == 2
    assert result.reply == repaired["decision"]["reply"]


def test_review_is_required_and_bound_to_reply_and_facts():
    provider = Provider(native(finish()), review())
    d = driver(provider)
    d.run_turn(BuyerAction(action="ASK", message="能讲价吗？"))
    backend = d.last_backend
    call = {"name": "finish_turn", "args": finish(), "id": "finish"}
    assert reviewed_reply(backend, call, backend.checkpoint).text == finish()["decision"]["reply"]
    relabeled = deepcopy(call)
    relabeled["args"]["decision"]["topics"] = ["更换辅助标签"]
    assert review_input(backend, relabeled)[0] == review_input(backend, call)[0]
    tampered = deepcopy(call)
    tampered["args"]["decision"]["reply"] = "商品有一年保修。"
    with pytest.raises(ValueError, match="bound to current evidence"):
        reviewed_reply(backend, tampered, backend.checkpoint)
    with pytest.raises(ValueError):
        reviewed_reply(backend, call, {"reply_reviews": {}})
    backend.request.product_context["product"]["description"] = "事实已变化"
    with pytest.raises(ValueError):
        reviewed_reply(backend, call, backend.checkpoint)


def test_claiming_seller_contact_is_blocked_even_when_judge_would_approve():
    provider = Provider(
        native(finish("我会帮您向卖家反馈并评估可行性。")),
        native(finish("可以讨论价格，具体条件需评估后确认。"), call_id="repair"),
        review(),
    )
    d = driver(provider)
    result = d.run_turn(BuyerAction(action="ASK", message="支持讲价吗？"))
    assert "反馈" not in result.reply
    assert provider.purposes == ["REACT", "REACT_ENDING", "REPLY_REVIEW"]


def test_review_does_not_consume_memory_or_private_policy_as_fact():
    provider = Provider(native(finish()), review())
    d = driver(provider)
    d.context["memory"] = {"hypotheses": ["已同意包邮"]}
    d.run_turn(BuyerAction(action="ASK", message="能讲价吗？"))
    payload = json.loads(provider.inputs[-1][-1].content)
    assert "memory" not in payload
    assert "minimum_net_price" not in json.dumps(payload)
    assert payload["conversation"][-1]["content"] == "能讲价吗？"


def test_json_ending_uses_open_contract_and_is_reviewed():
    provider = Provider(
        AIMessage(content="纯文本1"),
        AIMessage(content="纯文本2"),
        AIMessage(content=json.dumps(finish())),
        review(),
    )
    d = driver(provider)
    result = d.run_turn(BuyerAction(action="ASK", message="能讲价吗？"))
    assert result.outcome.value == "INFORMATIONAL"
    assert provider.purposes == ["REACT", "REACT_ENDING", "CONVERSATION_FINISH", "REPLY_REVIEW"]
    assert "dialogue_acts" not in json.dumps(response_format_contract("CONVERSATION_FINISH"))


def test_budget_exhaustion_prevents_unreviewed_publication():
    provider = Provider(native(finish()))
    d = driver(provider)
    d.config["budget"]["total_calls"] = 1
    result = d.run_turn(BuyerAction(action="ASK", message="能讲价吗？"))
    assert result.outcome.value == "SAFE_FAILURE"
    assert result.reply != finish()["decision"]["reply"]
    assert provider.purposes == ["REACT"]


def test_thinking_main_loop_uses_json_finish_without_forced_named_tool():
    provider = Provider(
        AIMessage(content="纯文本1"),
        AIMessage(content="纯文本2"),
        AIMessage(content=json.dumps(finish())),
        review(),
    )
    d = driver(provider, thinking=True)
    result = d.run_turn(BuyerAction(action="ASK", message="能讲价吗？"))
    assert result.outcome.value == "INFORMATIONAL"
    assert provider.purposes == ["REACT", "REACT", "CONVERSATION_FINISH", "REPLY_REVIEW"]
    assert d.call.calls[0]["request"]["model"]["thinking"] is True
    assert d.call.calls[0]["request"]["model"]["thinking_budget"] == 768


def test_disabled_thinking_applies_to_main_and_review_with_review_still_required():
    provider = Provider(native(finish()), review())
    d = driver(provider)
    d.config = freeze_config(
        get_settings().model_copy(
            update={
                "agent_reply_strategy": "conversation-v1",
                "model_enable_thinking": False,
                "agent_reply_review_enable_thinking": None,
            }
        )
    )
    d.config["budget"]["estimated_cost"] = None
    d.call.config = d.config
    result = d.run_turn(BuyerAction(action="ASK", message="是否支持讲价？"))
    assert result.outcome.value == "INFORMATIONAL"
    assert provider.purposes == ["REACT", "REPLY_REVIEW"]
    assert all(c["request"]["model"]["thinking"] is False for c in d.call.calls)


def test_checkpoint_reuses_review_without_another_rpc():
    provider = Provider(native(finish()), review())
    d = driver(provider)
    d.run_turn(BuyerAction(action="ASK", message="能讲价吗？"))
    backend = d.last_backend
    runtime = SellerAgentRuntime(backend)
    call = {"name": "finish_turn", "args": finish(), "id": "finish"}
    assert runtime._review_reply(call) is None
    assert provider.purposes == ["REACT", "REPLY_REVIEW"]
    key, _ = review_input(backend, call)
    assert backend.checkpoint["reply_reviews"][key]["approved"]


def test_frozen_historical_tasks_keep_old_contract():
    assert not conversational({})
    old = finish()
    old["decision"].pop("topics")
    old["decision"]["dialogue_acts"] = [{"kind": "GENERAL", "subject": "GENERAL"}]
    assert parse_finish({}, old).business_decision().dialogue_acts
    with pytest.raises(ValueError):
        ConversationFinish.model_validate(old)


@pytest.mark.parametrize(
    "path,quote",
    [
        ("product.product.warranty", "一年"),
        ("product.product.description", "一年保修"),
        ("product.product", "title"),
    ],
)
def test_judge_cannot_approve_with_invented_or_broad_fact_reference(path, quote):
    dishonest_review = AIMessage(
        content=json.dumps(
            {
                "approved": True,
                "reason": "错误地批准无来源保修",
                "fact_checks": [
                    {
                        "claim": "还有一年保修",
                        "evidence_path": path,
                        "evidence_quote": quote,
                    }
                ],
            }
        )
    )
    corrected = finish("保修期限未记录，需要卖家确认。")
    provider = Provider(
        native(finish("还有一年保修。")),
        dishonest_review,
        native(corrected, call_id="repair"),
        review(),
    )
    d = driver(provider)
    result = d.run_turn(BuyerAction(action="ASK", message="还有多久保修？"))
    assert result.reply == corrected["decision"]["reply"]
    assert any(not r["approved"] for r in d.last_backend.checkpoint["reply_reviews"].values())


def test_formal_offer_constraint_cannot_be_bypassed_by_reviewed_consultation():
    provider = Provider(native(finish("可以讨论价格。")), review())
    d = driver(provider)
    result = d.run_turn(
        BuyerAction(
            action="OFFER",
            message="我正式出价1元",
            offer=OfferSpec(price="1", shipping_paid_by="buyer"),
        )
    )
    assert result.outcome.value == "REJECTED"
    assert result.reply != "可以讨论价格。"
    assert not d.environment.commitments


def test_authorized_counter_keeps_business_renderer_without_dialogue_review():
    provider = Provider(
        native({"price": "3000", "shipping_paid_by": "buyer"}, "submit_counter_offer", "counter")
    )
    d = driver(provider)
    result = d.run_turn(BuyerAction(action="ASK", message="给个正式报价"))
    assert result.outcome.value == "COUNTER_OFFERED" and "3000.00" in result.reply
    assert provider.purposes == ["REACT"]


def test_rejected_review_retains_specific_reason_even_when_evidence_is_missing():
    rejected = AIMessage(
        content=json.dumps(
            {
                "approved": False,
                "reason": "商品没有照片数量记录，不能声称只有一张主图",
                "fact_checks": [
                    {"claim": "只有一张主图", "evidence_path": "", "evidence_quote": ""}
                ],
            }
        )
    )
    corrected = finish("照片需要联系卖家提供。")
    provider = Provider(
        native(finish("只有一张主图。")), rejected, native(corrected, call_id="repair"), review()
    )
    d = driver(provider)
    assert (
        d.run_turn(BuyerAction(action="ASK", message="有近照吗？")).reply
        == corrected["decision"]["reply"]
    )
    assert any(
        "不能声称只有一张主图" in str(r.payload) for r in d.stream if r.kind == "TOOL_RESULT"
    )


def test_truncated_review_is_never_used_even_if_json_happens_to_parse():
    truncated = review()
    truncated.response_metadata["structured_output_truncated"] = True
    corrected = finish("可以商量，具体条件评估后确认。")
    provider = Provider(native(finish()), truncated, native(corrected, call_id="repair"), review())
    d = driver(provider)
    assert (
        d.run_turn(BuyerAction(action="ASK", message="能讲价吗？")).reply
        == corrected["decision"]["reply"]
    )
    assert any(not v["approved"] for v in d.last_backend.checkpoint["reply_reviews"].values())


def test_provider_preserves_truncated_completion_text_and_usage(monkeypatch):
    from openai import LengthFinishReasonError
    from openai.types.chat import ChatCompletion

    from app.agent import decision_provider as module

    completion = ChatCompletion.model_validate(
        {
            "id": "truncated",
            "created": 0,
            "model": "test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": '{"approved":',
                    },
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }
    )

    class Model:
        openai_api_base = None

        def model_copy(self, **kwargs):
            return self

        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            raise LengthFinishReasonError(completion=completion)

    monkeypatch.setattr(module, "create_agent", lambda **kwargs: None)
    config = freeze_config(get_settings())
    config["model"]["endpoint"] = None
    response = module.LangChainDecisionProvider(Model()).invoke_agent(
        [], [], config, purpose="REPLY_REVIEW"
    )
    assert response.content == '{"approved":'
    assert response.usage_metadata["total_tokens"] == 120
    assert response.response_metadata["structured_output_truncated"]


def test_internal_terms_are_repaired_without_disclosing_implementation():
    corrected = finish("卖家的私有价格信息不便透露，您可以说说期望价格。")
    provider = Provider(
        native(finish("不能透露自动接受阈值。")), native(corrected, call_id="repair"), review()
    )
    d = driver(provider)
    result = d.run_turn(BuyerAction(action="ASK", message="底价是多少？"))
    assert result.reply == corrected["decision"]["reply"]
    assert provider.purposes == ["REACT", "REACT_ENDING", "REPLY_REVIEW"]
