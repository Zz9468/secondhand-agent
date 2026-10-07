import json
from copy import deepcopy

from langchain_core.messages import ToolMessage

from app.agent.context_builder import ContextBuilder
from app.agent.conversation_reply import parse_finish, reviewed_reply
from app.agent.decision import NegotiationDecision
from app.agent.decision_provider import ConversationMessage
from app.agent.message_protocol import ContextState, StreamRecord, encode_message
from app.agent.negotiation_memory import MemoryUpdater, record_memory_failure
from app.agent.prompts import DECISION_FIELD_RULES, SELLER_AGENT_SYSTEM_PROMPT
from app.agent.reply_policy import FormalReplyRenderer
from app.agent.runtime import SellerAgentRuntime
from app.agent.seller_agent import (
    AgentTurnOutcome,
    AgentTurnPreparation,
    AgentTurnResult,
    SellerAgent,
)
from app.agent.tools.gateway import search_history, tool_schemas, visible_tool_result


class MemoryRuntimeBackend:
    def __init__(self, driver, request):
        self.driver, self.request = driver, request
        self.config = driver.config
        self.current_input_seq = driver.stream[-1].seq
        self.initial_context = deepcopy(driver.context)
        self.checkpoint = {}
        self.tool_schemas = tool_schemas(driver.environment.tools(), self.config)
        self.task_id = driver.turn

    def ensure_current(self):
        return True

    def facts(self):
        return {
            "product": self.request.product_context,
            "negotiation": self.request.negotiation_context,
            "current_turn_offer_id": self.request.current_turn_offer_id,
            **(
                {"assistant_capabilities": self.config["assistant_capabilities"]}
                if self.config.get("assistant_capabilities")
                else {}
            ),
        }

    def records(self, branch_id):
        return self.driver.stream[:]

    def cached_response(self, purpose, checkpoint):
        return None

    def save_checkpoint(self, checkpoint):
        self.checkpoint = deepcopy(checkpoint)

    def save_step(self, kind, payload, checkpoint):
        self.driver.append(
            kind, payload, "INTERNAL", task_id=self.task_id, branch_id=checkpoint["branch_id"]
        )
        self.save_checkpoint(checkpoint)

    def call_model(self, messages, tools, purpose, manifest, checkpoint):
        return self.driver.call(messages, tools, purpose=purpose, manifest=manifest)

    def _result(self, call, value, cp):
        message = ToolMessage(
            content=json.dumps(
                visible_tool_result(value, self.config["budget"]["tool_result_tokens"]),
                ensure_ascii=False,
                default=str,
            ),
            tool_call_id=call["id"],
            name=call["name"],
            artifact={"full_result": value},
        )
        self.save_step("TOOL_RESULT", {"message": encode_message(message)}, cp)
        if self.config["memory_enabled"]:
            cp["memory"] = MemoryUpdater.record_tool(
                cp.get("memory", self.initial_context.get("memory", {})),
                self.driver.stream[-1],
                call,
                value,
            )
        cp["pending_calls"] = cp["pending_calls"][1:]
        self.save_checkpoint(cp)

    def finish(self, result, cp, memory=None):
        if memory is None and cp.get("memory") is not None:
            memory = cp["memory"]
        cp["stop_reason"] = cp.get("stop_reason", "COMPLETED")
        self.driver.append(
            "PUBLIC_REPLY",
            {
                "content": result.reply,
                "outcome": result.outcome.value,
                "stop_reason": cp["stop_reason"],
            },
            "PUBLIC",
        )
        if cp.get("summary"):
            self.driver.context.update(
                summary=cp["summary"],
                covered_through=cp["covered_through"],
                summary_sources=cp.get("summary_sources", []),
            )
        if memory is not None and self.config["memory_enabled"]:
            self.driver.context["memory"] = memory
            self.driver.context["memory_covered_through"] = cp.get(
                "memory_covered_through", self.initial_context.get("memory_covered_through", 0)
            )
        self.save_checkpoint(cp)
        return result

    def execute(self, call, cp, *, protocol_error=None):
        result = None
        memory = None
        env = self.driver.environment
        try:
            if protocol_error:
                raise ValueError(protocol_error)
            name, args = call["name"], call["args"]
            if name == "finish_turn":
                finish = parse_finish(self.config, args)
                finish.validate_non_write()
                dialogue_reply = reviewed_reply(self, call, cp)
                try:
                    memory = (
                        MemoryUpdater.apply(
                            cp.get("memory", self.initial_context.get("memory", {})),
                            finish.memory_delta,
                            self.records(cp["branch_id"]),
                        )
                        if self.config["memory_enabled"]
                        else None
                    )
                    if not cp.get("memory_refresh_incomplete"):
                        cp["memory_covered_through"] = self.current_input_seq
                except ValueError as exc:
                    record_memory_failure(
                        cp,
                        "FINISH_DELTA",
                        exc,
                        [seq for entry in finish.memory_delta.entries for seq in entry.source_seqs],
                    )
                result = self.driver.agent.apply_prepared(
                    AgentTurnPreparation(decision_request=self.request),
                    decision=finish.business_decision(),
                    validated_dialogue_reply=dialogue_reply,
                )
                value = {
                    "ok": True,
                    "outcome": result.outcome.value,
                    "topics": getattr(finish.decision, "topics", []),
                    "reply_reviewed": dialogue_reply is not None,
                }
            elif name == "search_conversation_history":
                value = search_history(self.records(cp["branch_id"]), args)
            else:
                if (
                    name in {"accept_offer", "request_approval"}
                    and args.get("offer_id") != env.current_turn_offer_id
                ):
                    raise ValueError("Only the current formal buyer offer is authorized")
                tools = {t.name: t for t in env.tools()}
                value = tools[name].invoke(args)
                if value.get("ok") and name == "submit_counter_offer":
                    result = AgentTurnResult(
                        FormalReplyRenderer().render_counter_offer(value),
                        AgentTurnOutcome.COUNTER_OFFERED,
                        None,
                        value["offer"]["id"],
                    )
                elif value.get("ok") and name == "accept_offer":
                    result = AgentTurnResult(
                        FormalReplyRenderer().render_accepted_offer(value),
                        AgentTurnOutcome.OFFER_ACCEPTED,
                        None,
                        value["offer"]["id"],
                    )
                elif value.get("ok") and name == "request_approval":
                    result = AgentTurnResult(
                        "已将这份报价提交卖家确认，目前还不能视为接受或成交，请等待后续结果。",
                        AgentTurnOutcome.NEEDS_SELLER_CONFIRMATION,
                        None,
                    )
        except (ValueError, KeyError, TypeError) as exc:
            value = {
                "ok": False,
                "error": {"code": "TOOL_INPUT_REJECTED", "message": str(exc)[:300]},
            }
        self._result(call, value, cp)
        return self.finish(result, cp, memory) if result else None

    def stop(self, reason, checkpoint):
        for call in list(checkpoint["pending_calls"]):
            self._result(call, {"ok": False, "error": {"code": reason}}, checkpoint)
        checkpoint["stop_reason"] = reason
        return self.finish(
            AgentTurnResult(
                "当前条件暂时无法安全处理，请补充交易条件或稍后再试。",
                AgentTurnOutcome.SAFE_FAILURE,
                None,
            ),
            checkpoint,
        )


class ProductionDriver:
    """Uses the actual SellerAgent policy/renderer and V4 Runtime without database writes."""

    def __init__(self, environment, config, group, call):
        self.environment, self.config, self.group, self.call = (
            environment,
            deepcopy(config),
            group,
            call,
        )
        self.config["memory_enabled"] = group in {"G3", "G4"}
        self.stream = []
        self.context = {"summary": "", "covered_through": 0, "memory": {}, "summary_sources": []}
        self.turn = 0
        self.agent = SellerAgent(decision_provider=None, tools=environment.tools())
        self.last_backend = None
        self.turn_backends = []

    def append(self, kind, payload, visibility="INTERNAL", task_id=None, branch_id=None):
        self.stream.append(
            StreamRecord(len(self.stream) + 1, kind, payload, task_id, branch_id, visibility)
        )
        return self.stream[-1]

    def run_turn(self, action):
        self.turn += 1
        self.last_backend = None
        self.environment.current_turn_offer_id = None
        if action.action == "OFFER":
            self.environment.buyer_offer(action.offer)
        history = tuple(
            ConversationMessage(
                role="BUYER" if r.kind == "BUYER_INPUT" else "AGENT",
                content=r.payload["content"],
            )
            for r in self.stream
            if r.visibility == "PUBLIC"
        )
        self.append("BUYER_INPUT", {"content": action.message}, "PUBLIC")
        preparation = self.agent.prepare_turn(
            action.message,
            conversation_history=history,
            current_turn_offer_id=self.environment.current_turn_offer_id,
        )
        if not preparation.requires_model:
            result = self.agent.apply_prepared(preparation)
            self.append(
                "RUNTIME_EVENT", {"route": "DETERMINISTIC", "outcome": result.outcome.value}
            )
            self.append("PUBLIC_REPLY", {"content": result.reply}, "PUBLIC")
            return result
        request = preparation.decision_request
        backend = MemoryRuntimeBackend(self, request)
        self.last_backend = backend
        self.turn_backends.append(backend)
        if self.group in {"G2", "G4"}:
            return SellerAgentRuntime(backend).run()
        # Baselines retain candidate execution and the actual production reply policy.
        config = deepcopy(self.config)
        config["prompt"]["text"] = SELLER_AGENT_SYSTEM_PROMPT
        records = (
            self.stream[:]
            if self.group != "G0"
            else [r for r in self.stream if r.visibility == "PUBLIC"][-21:]
        )
        state = ContextState(
            summary_sources=self.context["summary_sources"] if self.group != "G0" else [],
            summary=self.context["summary"] if self.group != "G0" else "",
            covered_through=self.context["covered_through"] if self.group != "G0" else 0,
            memory=self.context["memory"] if self.config["memory_enabled"] else {},
        )
        runtime = SellerAgentRuntime(backend)
        compressor = None if self.group == "G0" else runtime._compress
        from app.agent.decision_provider import budget_contract

        messages, manifest, state = ContextBuilder(config).build(
            records,
            state,
            backend.facts(),
            budget_contract([], "CANDIDATE"),
            current_input_seq=backend.current_input_seq,
            compressor=compressor,
        )
        response = runtime.call(messages, [], purpose="CANDIDATE", manifest=manifest)
        self.append("MODEL_CANDIDATE", {"message": encode_message(response)})
        try:
            decision = NegotiationDecision.model_validate_json(response.content)
        except ValueError as exc:
            from langchain_core.messages import SystemMessage

            response = runtime.call(
                [
                    *messages,
                    SystemMessage(
                        content="上一份决策字段校验失败："
                        + str(exc)[:600]
                        + "\n"
                        + DECISION_FIELD_RULES
                    ),
                ],
                [],
                purpose="CANDIDATE",
                manifest={**manifest, "repair": True},
            )
            self.append("MODEL_CANDIDATE", {"message": encode_message(response)})
            decision = NegotiationDecision.model_validate_json(response.content)
        result = self.agent.apply_prepared(preparation, decision=decision)
        self.context.update(
            summary=state.summary,
            covered_through=state.covered_through,
            summary_sources=state.summary_sources,
        )
        self.append("PUBLIC_REPLY", {"content": result.reply}, "PUBLIC")
        if self.config["memory_enabled"]:
            from langchain_core.messages import HumanMessage, SystemMessage

            from app.agent.negotiation_memory import MemoryDelta

            try:
                memory_response = runtime.call(
                    [
                        SystemMessage(content=config["memory_prompt"]["text"]),
                        HumanMessage(
                            content=json.dumps(
                                {
                                    "memory": self.context["memory"],
                                    "memory_schema": MemoryDelta.model_json_schema(),
                                    "records": [
                                        {"seq": r.seq, "kind": r.kind, "payload": r.payload}
                                        for r in self.stream
                                        if r.visibility == "PUBLIC"
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        ),
                    ],
                    [],
                    purpose="MEMORY",
                    manifest={},
                )
                self.context["memory"] = MemoryUpdater.apply(
                    self.context["memory"],
                    MemoryDelta.model_validate_json(memory_response.content),
                    self.stream,
                )
            except Exception as exc:
                record_memory_failure(backend.checkpoint, "CANDIDATE_MEMORY", exc, [])
        return result
