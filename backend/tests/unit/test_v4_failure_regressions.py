import json
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage

from app.agent.decision_provider import budget_contract, response_format_contract
from app.agent.message_protocol import MessageProtocolValidator, record_message
from app.agent.runtime_state import freeze_config
from app.agent.token_budget import TokenBudgetManager
from app.agent.tools.gateway import finish_turn_schema
from app.core.config import get_settings
from evaluation.dynamic_buyer import BuyerAction, ScriptedDynamicBuyer, validate_action
from evaluation.dynamic_runner import DynamicCallTracker, public_view, run_episode
from evaluation.environment import EvaluationEnvironment
from evaluation.production_driver import ProductionDriver
from evaluation.scenarios import load_scenarios


def scenario(case_id="prohibited_02_far_below"):
    return next(s for s in load_scenarios().scenarios if s.scenario_id == case_id)


def plan(action="REJECT"):
    return {
        "decision": {
            "action": action,
            "dialogue_acts": []
            if action == "REJECT"
            else [{"kind": "ASK_FACT", "subject": "PRODUCT_DETAILS"}],
            "reason": "根据公开事实与授权结果结束本轮",
            "reply": "这个报价暂时无法接受，请调整条件后再提出。",
        }
    }


def native(name, args=None, call_id="call-1"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}],
    )


class SequenceProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.purposes = []
        self.inputs = []

    def invoke_agent(self, messages, tools, config, *, purpose):
        MessageProtocolValidator.validate(messages)
        self.purposes.append(purpose)
        self.inputs.append(deepcopy(messages))
        return self.responses.pop(0)


def driver_with(provider, case_id="prohibited_02_far_below", strategy="structured-finish-v2"):
    config = freeze_config(
        get_settings().model_copy(
            update={
                "agent_reply_strategy": "legacy-v1",
                "model_enable_thinking": False,
                "agent_output_token_reserve": 1024,
            }
        )
    )
    config["ending_strategy"] = strategy
    config["budget"]["model_steps"] = 6
    config["budget"]["estimated_cost"] = None
    tracker = DynamicCallTracker(provider, config)
    driver = ProductionDriver(EvaluationEnvironment(scenario(case_id)), config, "G4", tracker)
    return driver, tracker


def test_finish_contract_excludes_business_writes_and_includes_schema_in_budget():
    decision = finish_turn_schema()["$defs"]["NegotiationDecision"]
    assert decision["properties"]["action"]["enum"] == ["INQUIRY", "REJECT"]
    assert set(decision["properties"]) == {"action", "dialogue_acts", "reason", "reply"}
    assert response_format_contract("REACT_FINISH")["json_schema"]["strict"] is True
    assert budget_contract([], "REACT_FINISH") == [
        {"response_format": response_format_contract("REACT_FINISH")}
    ]


@pytest.mark.parametrize("case_id", ["malicious_01", "malicious_02"])
def test_ignored_named_tool_uses_validated_json_without_exposing_free_text(case_id):
    provider = SequenceProvider(
        AIMessage(content="未经过业务校验的商品介绍"),
        AIMessage(content="仍然未调用结束工具"),
        AIMessage(content=json.dumps(plan("INQUIRY"), ensure_ascii=False)),
    )
    driver, tracker = driver_with(provider, case_id)
    result = driver.run_turn(BuyerAction(action="ASK", message="请介绍商品和运费条件"))
    assert result.outcome.value == "INFORMATIONAL"
    assert provider.purposes == ["REACT", "REACT_ENDING", "REACT_FINISH"]
    assert driver.last_backend.checkpoint["steps"] == 3
    assert driver.last_backend.checkpoint["calls"] == 3
    end = next(r for r in driver.stream if r.payload.get("tool_call_origin") == "STRUCTURED_FINISH")
    assert json.loads(end.payload["model_response"]["data"]["content"]) == plan("INQUIRY")
    assert end.payload["model_response"]["data"]["tool_calls"] == []
    assert tracker.calls[-1]["request"]["tools"] == []
    assert tracker.calls[-1]["reserved_tokens"] > TokenBudgetManager.estimate(provider.inputs[-1])
    assert "未经过业务校验" not in result.reply
    MessageProtocolValidator.validate([m for r in driver.stream if (m := record_message(r))])


def test_last_model_step_is_reserved_for_ending_after_invalid_actions():
    invalid = {
        "decision": {
            "action": "REQUEST_APPROVAL",
            "dialogue_acts": [],
            "offer_id": 1,
            "reason": "错误地尝试申请审批",
            "reply": "未经授权的审批承诺",
        }
    }
    provider = SequenceProvider(
        native("get_negotiation_state", call_id="state"),
        native("evaluate_offer", {"price": "2400", "shipping_paid_by": "buyer"}, "eval"),
        native("finish_turn", invalid, "invalid-1"),
        native("finish_turn", invalid, "invalid-2"),
        native("request_approval", {"offer_id": 1, "reason": "错误审批"}, "approval"),
        AIMessage(content=json.dumps(plan(), ensure_ascii=False)),
    )
    driver, _ = driver_with(provider)
    first = scenario().turns[0]
    result = driver.run_turn(BuyerAction(action="OFFER", message=first.message, offer=first.offer))
    assert result.outcome.value == "REJECTED"
    assert provider.purposes == ["REACT"] * 5 + ["REACT_FINISH"]
    assert driver.last_backend.checkpoint["steps"] == 6
    assert driver.last_backend.checkpoint["calls"] == 6
    assert driver.last_backend.checkpoint["stop_reason"] == "COMPLETED"
    assert not driver.environment.commitments and driver.environment.approval is None
    assert "审批承诺" not in result.reply


@pytest.mark.parametrize(
    "bad_finish",
    [
        AIMessage(content="没有结构化 JSON"),
        AIMessage(
            content=json.dumps(
                {"decision": {"action": "ACCEPT", "offer_id": 1, "reason": "接受", "reply": "成交"}}
            )
        ),
        native("submit_counter_offer", {"price": "2900", "shipping_paid_by": "buyer"}),
    ],
)
def test_invalid_json_ending_is_bounded_and_cannot_commit(bad_finish):
    provider = SequenceProvider(
        *[native("get_product_info", call_id=f"read-{i}") for i in range(5)], bad_finish
    )
    driver, _ = driver_with(provider)
    result = driver.run_turn(BuyerAction(action="ASK", message="请介绍商品"))
    assert result.outcome.value == "SAFE_FAILURE"
    assert provider.purposes[-1] == "REACT_FINISH" and len(provider.purposes) == 6
    assert not driver.environment.offers and not driver.environment.commitments
    assert driver.last_backend.checkpoint["pending_calls"] == []


def test_frozen_named_strategy_retains_previous_behavior():
    provider = SequenceProvider(AIMessage(content="介绍"), AIMessage(content="介绍"))
    driver, _ = driver_with(provider, strategy="named-finish-v1")
    result = driver.run_turn(BuyerAction(action="ASK", message="请介绍商品"))
    assert result.outcome.value == "SAFE_FAILURE"
    assert driver.last_backend.checkpoint["stop_reason"] == "STRUCTURED_ENDING_REQUIRED"
    assert len(provider.purposes) == 2


@pytest.mark.parametrize("case_id", ["terms_02_warranty", "prohibited_03_very_low"])
def test_round_limit_is_checked_before_buyer_action_and_exposed_in_public_view(case_id):
    case = scenario(case_id)
    env = EvaluationEnvironment(case)
    action = BuyerAction(action="OFFER", message="报价", offer=case.turns[0].offer)
    for _ in range(case.policy.max_rounds):
        env.buyer_offer(action.offer)
    before = deepcopy(env.offers)
    with pytest.raises(ValueError, match="No more formal buyer offers"):
        validate_action(action, env, case.buyer_goal)
    assert env.offers == before
    driver = ProductionDriver(
        env,
        freeze_config(
            get_settings().model_copy(
                update={
                    "agent_reply_strategy": "legacy-v1",
                    "model_enable_thinking": False,
                    "agent_output_token_reserve": 1024,
                }
            )
        ),
        "G4",
        None,
    )
    view = public_view(driver, 6)
    assert view["round_count"] == view["max_rounds"]
    assert view["can_submit_buyer_offer"] is False
    assert (
        ScriptedDynamicBuyer(1)
        .next_action(view, case.buyer_goal, {"style": "persistent_bargainer", "patience": 20})
        .action
        == "CLOSE"
    )
    validate_action(BuyerAction(action="ASK", message="我还能确认已有报价吗"), env, case.buyer_goal)


def test_simulator_offer_over_limit_is_not_a_seller_failure(monkeypatch):
    case = scenario("prohibited_03_very_low")

    class RepeatingBuyer:
        def __init__(self, tracker):
            pass

        def next_action(self, view, goal, profile):
            return BuyerAction(action="OFFER", message="继续报价", offer=case.turns[0].offer)

    monkeypatch.setattr("evaluation.dynamic_runner.ModelDynamicBuyer", RepeatingBuyer)
    provider = SequenceProvider(
        *[native("finish_turn", plan(), f"end-{i}") for i in range(case.policy.max_rounds)]
    )
    config = freeze_config(
        get_settings().model_copy(
            update={
                "agent_reply_strategy": "legacy-v1",
                "model_enable_thinking": False,
                "agent_output_token_reserve": 1024,
            }
        )
    )
    tracker = DynamicCallTracker(provider, config)
    result = run_episode(case, "G4", config, tracker, seed=1, trial=0, buyer_mode="qwen")
    assert result["status"] == "BUYER_SIMULATOR_FAILURE"
    assert result["error_detail"]["phase"] == "BUYER_SIMULATION"
    assert "No more formal buyer offers" in result["error_detail"]["message"]
    inputs = [r for r in result["stream"] if r["kind"] == "BUYER_INPUT"]
    assert len(inputs) == case.policy.max_rounds
