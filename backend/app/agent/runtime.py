import json
from datetime import UTC, datetime
from decimal import Decimal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.context_builder import ContextBuilder
from app.agent.context_compressor import ContextCompressor
from app.agent.conversation_reply import (
    ReplyReview,
    conversational,
    parse_finish,
    preliminary_error,
    review_input,
    review_messages,
    validate_review_evidence,
)
from app.agent.decision_provider import budget_contract
from app.agent.message_protocol import ContextState, encode_message
from app.agent.negotiation_memory import MemoryDelta, MemoryUpdater, record_memory_failure
from app.agent.runtime_state import AgentBudgetExceeded, call_budget
from app.agent.token_budget import TokenBudgetManager
from app.agent.tools.gateway import batch_error


class SellerAgentRuntime:
    """Same bounded execution loop for production, recovery and dynamic evaluation."""

    def __init__(self, backend):
        self.backend = backend
        self.config = backend.config
        self.builder = ContextBuilder(self.config)
        self.cp = backend.checkpoint
        self.cp.setdefault("branch_id", "branch-1")
        self.cp.setdefault("steps", 0)
        self.cp.setdefault("calls", 0)
        self.cp.setdefault("tools", 0)
        self.cp.setdefault("reserved_tool_ids", [])
        self.cp.setdefault("maintenance_calls", 0)
        self.cp.setdefault("reserved_tokens", 0)
        self.cp.setdefault("reserved_cost", "0")
        self.cp.setdefault("call_reservations", {})
        self.cp.setdefault("pending_calls", [])
        self.cp.setdefault("started_at", datetime.now(UTC).isoformat())
        self.state = ContextState(
            summary=self.cp.get("summary", backend.initial_context.get("summary", "")),
            covered_through=self.cp.get(
                "covered_through", backend.initial_context.get("covered_through", 0)
            ),
            memory=self.cp.get("memory", backend.initial_context.get("memory", {}))
            if self.config["memory_enabled"]
            else {},
            summary_sources=self.cp.get(
                "summary_sources", backend.initial_context.get("summary_sources", [])
            ),
        )

    def _check_wall(self):
        elapsed = (
            datetime.now(UTC) - datetime.fromisoformat(self.cp["started_at"])
        ).total_seconds()
        if elapsed >= self.config["budget"]["wall_seconds"]:
            raise AgentBudgetExceeded("WALL_TIME")

    def call(self, messages, tools, *, purpose, manifest):
        budget = call_budget(self.config, purpose)
        self._check_wall()
        reservation_key = (
            f"{purpose}:{self.cp['steps'] + 1}"
            if purpose in {"REACT", "REACT_ENDING", "REACT_FINISH", "CONVERSATION_FINISH"}
            else f"REPLY_REVIEW:{self.cp['reply_review_key']}"
            if purpose == "REPLY_REVIEW"
            else f"{purpose}:call:{self.cp['calls'] + 1}"
        )
        cached = self.backend.cached_response(purpose, self.cp)
        if cached is not None:
            self._reconcile_usage(cached, reservation_key)
            return cached
        try:
            count = TokenBudgetManager(budget).check(messages, budget_contract(tools, purpose))
        except ValueError as exc:
            raise AgentBudgetExceeded("CALL_CONTEXT_BUDGET") from exc
        reserve = count + budget["output_tokens"]
        prices = self.config.get("prices", {})
        call_cost = (
            Decimal(count) * Decimal(prices.get("input") or "0")
            + Decimal(budget["output_tokens"]) * Decimal(prices.get("output") or "0")
        ) / Decimal("1000000")
        if budget.get("estimated_cost") and (
            not prices.get("input")
            or not prices.get("output")
            or Decimal(self.cp["reserved_cost"]) + call_cost > Decimal(budget["estimated_cost"])
        ):
            raise AgentBudgetExceeded("COST_BUDGET_OR_MISSING_PRICE")
        if (
            self.cp["calls"] >= budget["total_calls"]
            or self.cp["reserved_tokens"] + reserve > budget["total_tokens"]
        ):
            raise AgentBudgetExceeded("MODEL_CALL_OR_TOKEN_BUDGET")
        if purpose in {"SUMMARY", "MEMORY"}:
            if self.cp["maintenance_calls"] >= budget["maintenance_calls"]:
                raise AgentBudgetExceeded("MAINTENANCE_BUDGET")
            self.cp["maintenance_calls"] += 1
        self.cp["calls"] += 1
        self.cp["reserved_tokens"] += reserve
        self.cp["reserved_cost"] = str(Decimal(self.cp["reserved_cost"]) + call_cost)
        self.cp["call_reservations"][reservation_key] = {
            "tokens": reserve,
            "cost": str(call_cost),
            "reconciled": False,
        }
        self.backend.save_checkpoint(self.cp)
        response = self.backend.call_model(messages, tools, purpose, manifest, self.cp)
        self._reconcile_usage(response, reservation_key)
        return response

    def _review_reply(self, call):
        finish = parse_finish(self.config, call["args"])
        finish.validate_non_write()
        key, payload = review_input(self.backend, call)
        reviews = self.cp.setdefault("reply_reviews", {})
        if key not in reviews:
            error = preliminary_error(finish)
            if error:
                verdict = ReplyReview(approved=False, reason=error, fact_checks=[])
            else:
                self.cp["reply_review_key"] = key
                try:
                    response = self.call(
                        review_messages(self.config, payload),
                        [],
                        purpose="REPLY_REVIEW",
                        manifest={
                            "reply_review_key": key,
                            "current_input_seq": self.backend.current_input_seq,
                        },
                    )
                    if (
                        response.tool_calls
                        or response.invalid_tool_calls
                        or response.response_metadata.get("structured_output_truncated")
                    ):
                        raise ValueError("审核输出被截断或调用了工具，请修正并缩短候选回复")
                    verdict = ReplyReview.model_validate_json(response.content)
                    validate_review_evidence(verdict, payload["facts"])
                except ValueError as exc:
                    verdict = ReplyReview(
                        approved=False,
                        reason="回复审核或事实引用无效：" + str(exc)[:250],
                        fact_checks=[],
                    )
            reviews[key] = verdict.model_dump()
            self.backend.save_checkpoint(self.cp)
        if reviews[key]["approved"] is not True:
            self.cp["repair"] = True
            self.cp["reply_repair_feedback"] = (
                reviews[key]["reason"] or "回复被拒绝，请缩短并修正文案"
            )
            return "REPLY_REVIEW_REJECTED: " + reviews[key]["reason"]
        self.cp.pop("reply_repair_feedback", None)
        return None

    def _reconcile_usage(self, response, reservation_key):
        reservation = self.cp["call_reservations"].get(reservation_key)
        usage = response.usage_metadata or {}
        if not reservation or reservation["reconciled"]:
            return
        if not {"input_tokens", "output_tokens"}.issubset(usage):
            return
        actual_tokens = max(
            usage["input_tokens"] + usage["output_tokens"], usage.get("total_tokens", 0)
        )
        prices = self.config.get("prices", {})
        # Keep the original upper-bound reservation; only add actual overages.
        # Cached-input discounts may reduce invoices but do not release budget.
        actual_cost = (
            Decimal(usage["input_tokens"]) * Decimal(prices.get("input") or "0")
            + Decimal(usage["output_tokens"]) * Decimal(prices.get("output") or "0")
        ) / Decimal("1000000")
        self.cp["reserved_tokens"] += max(0, actual_tokens - reservation["tokens"])
        self.cp["reserved_cost"] = str(
            Decimal(self.cp["reserved_cost"])
            + max(Decimal("0"), actual_cost - Decimal(reservation["cost"]))
        )
        reservation["reconciled"] = True
        self.backend.save_checkpoint(self.cp)

    def _compress(self, *args):
        try:
            state = ContextCompressor(self.config, self.call).compress(*args)
            self.cp.update(
                summary=state.summary,
                covered_through=state.covered_through,
                summary_sources=state.summary_sources,
            )
            self.backend.save_checkpoint(self.cp)
            return state
        except AgentBudgetExceeded:
            return self.state
        except Exception as exc:
            from app.services.errors import ModelExecutionTaskLeaseError

            if isinstance(exc, ModelExecutionTaskLeaseError):
                raise
            return self.state

    def _refresh_memory(self, records):
        if not self.config["memory_enabled"] or self.cp.get("memory_refresh_done"):
            return
        self.cp["memory_refresh_done"] = True
        coverage = self.cp.get(
            "memory_covered_through", self.backend.initial_context.get("memory_covered_through", 0)
        )
        pending = [
            r
            for r in records
            if coverage < r.seq < self.backend.current_input_seq and r.visibility == "PUBLIC"
        ]
        if not any(r.kind == "BUYER_INPUT" for r in pending):
            return
        contiguous = self.config.get("memory_catchup_strategy") == "contiguous-prefix-v1"
        last_pending_seq = pending[-1].seq
        if contiguous:
            self.cp["memory_refresh_incomplete"] = True

        def messages():
            return [
                SystemMessage(content=self.config["memory_prompt"]["text"]),
                HumanMessage(
                    content=json.dumps(
                        {
                            "memory": self.state.memory,
                            "memory_schema": MemoryDelta.model_json_schema(),
                            "records": [
                                {"seq": r.seq, "kind": r.kind, "payload": r.payload}
                                for r in pending
                            ],
                        },
                        ensure_ascii=False,
                    )
                ),
            ]

        while (
            pending
            and self.builder.budget.estimate(messages(), budget_contract([], "MEMORY"))
            > self.builder.budget.limit
        ):
            # Only a processed contiguous prefix may advance the coverage cursor.
            pending.pop() if contiguous else pending.pop(0)
        if not pending:
            return
        try:
            response = self.call(
                messages(), [], purpose="MEMORY", manifest={"source_seqs": [r.seq for r in pending]}
            )
            self.state.memory = MemoryUpdater.apply(
                self.state.memory, MemoryDelta.model_validate_json(response.content), records
            )
            self.cp.update(memory=self.state.memory, memory_covered_through=pending[-1].seq)
            if contiguous:
                self.cp["memory_refresh_incomplete"] = pending[-1].seq != last_pending_seq
        except Exception as exc:
            from app.services.errors import ModelExecutionTaskLeaseError

            if isinstance(exc, ModelExecutionTaskLeaseError):
                raise
            record_memory_failure(self.cp, "CATCHUP", exc, [r.seq for r in pending])
        self.backend.save_checkpoint(self.cp)

    def run(self):
        while True:
            try:
                self._check_wall()
                if not self.backend.ensure_current():
                    return self.backend.stop("BUSINESS_STATE_CHANGED", self.cp)
                if self.cp["pending_calls"]:
                    call = self.cp["pending_calls"][0]
                    if call["id"] not in self.cp["reserved_tool_ids"]:
                        if self.cp["tools"] >= self.config["budget"]["tool_calls"]:
                            raise AgentBudgetExceeded("TOOL_BUDGET")
                        self.cp["tools"] += 1
                        self.cp["reserved_tool_ids"].append(call["id"])
                        self.backend.save_checkpoint(self.cp)
                    error = self.cp.get("batch_error")
                    if not error and call["name"] == "finish_turn" and conversational(self.config):
                        try:
                            error = self._review_reply(call)
                        except (ValueError, TypeError, KeyError) as exc:
                            error = str(exc)[:300]
                    result = self.backend.execute(call, self.cp, protocol_error=error)
                    if result is not None:
                        return result
                    continue
                if self.cp["steps"] >= self.config["budget"]["model_steps"]:
                    raise AgentBudgetExceeded("MODEL_STEP_BUDGET")
                records = self.backend.records(self.cp["branch_id"])
                self._refresh_memory(records)
                if self.config["memory_enabled"]:
                    self.state.memory = self.cp.get("memory", self.state.memory)
                purpose = "REACT"
                tools = self.backend.tool_schemas
                structured_finish = self.config.get("ending_strategy") == "structured-finish-v2"
                if (
                    self.cp.get("repair")
                    and self.config.get("ending_strategy")
                    in {"named-finish-v1", "structured-finish-v2"}
                    and not self.config["model"]["thinking"]
                ):
                    purpose = "REACT_ENDING"
                    tools = [t for t in tools if t["function"]["name"] == "finish_turn"]
                if structured_finish and (
                    self.cp.get("ending_json_required")
                    or self.cp["steps"] == self.config["budget"]["model_steps"] - 1
                ):
                    purpose, tools = "REACT_FINISH", []
                    if conversational(self.config):
                        purpose = "CONVERSATION_FINISH"
                messages, manifest, self.state = self.builder.build(
                    records,
                    self.state,
                    self.backend.facts(),
                    budget_contract(tools, purpose),
                    current_input_seq=self.backend.current_input_seq,
                    repair=bool(self.cp.get("repair")),
                    structured_finish=purpose in {"REACT_FINISH", "CONVERSATION_FINISH"},
                    reply_feedback=self.cp.get("reply_repair_feedback"),
                    compressor=self._compress,
                    growth_reserve=self.config["budget"]["growth_reserve"]
                    if not self.cp["steps"]
                    else 0,
                )
                if (
                    not self.cp["steps"]
                    and manifest["estimated_input_tokens"] + self.config["budget"]["growth_reserve"]
                    > self.builder.budget.limit
                ):
                    # Per-call validation remains authoritative.
                    manifest["initial_growth_reserve_available"] = (
                        self.builder.budget.limit - manifest["estimated_input_tokens"]
                    )
                response = self.call(messages, tools, purpose=purpose, manifest=manifest)
                if not isinstance(response, AIMessage):
                    raise ValueError("Invalid model response")
                raw_finish = None
                if purpose in {"REACT_FINISH", "CONVERSATION_FINISH"}:
                    try:
                        if response.tool_calls or response.invalid_tool_calls:
                            raise ValueError("Structured finish cannot call business tools")
                        if response.response_metadata.get("structured_output_truncated"):
                            raise ValueError("Structured finish was truncated")
                        finish = parse_finish(self.config, json.loads(response.content))
                        finish.validate_non_write()
                    except ValueError as exc:
                        self.cp["steps"] += 1
                        self.cp["repair"] = True
                        self.backend.save_step(
                            "MODEL_CANDIDATE",
                            {
                                "message": encode_message(AIMessage(content=str(exc)[:600])),
                                "native_response_rejected": True,
                                "model_response": encode_message(response),
                            },
                            self.cp,
                        )
                        continue
                    raw_finish = encode_message(response)
                    # A validated JSON plan becomes an explicit internal tool envelope.
                    # Its origin and actual model output remain recorded separately.
                    response = AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": (
                                    f"structured-finish-{self.backend.current_input_seq}-"
                                    f"{self.cp['branch_id']}-{self.cp['steps'] + 1}"
                                ),
                                "name": "finish_turn",
                                "args": finish.model_dump(mode="json"),
                                "type": "tool_call",
                            }
                        ],
                    )
                protocol_issue = (
                    "Malformed native tool call arguments" if response.invalid_tool_calls else None
                )
                if (
                    purpose == "REACT_ENDING"
                    and response.tool_calls
                    and [c["name"] for c in response.tool_calls] != ["finish_turn"]
                ):
                    protocol_issue = "Structured ending must use only finish_turn"
                if not response.tool_calls and self.cp.get("repair"):
                    if not structured_finish:
                        return self.backend.stop("STRUCTURED_ENDING_REQUIRED", self.cp)
                    self.cp["ending_json_required"] = True
                if response.tool_calls:
                    ids = [c["id"] for c in response.tool_calls]
                    prior = {
                        r.payload["message"]["data"].get("tool_call_id")
                        for r in records
                        if r.kind == "TOOL_RESULT"
                    }
                    if len(ids) != len(set(ids)) or any(
                        not x or len(x) > 128 or x in prior for x in ids
                    ):
                        protocol_issue = "Repeated, missing or oversized tool call ID"
                if protocol_issue:
                    self.cp["steps"] += 1
                    self.cp["repair"] = True
                    self.backend.save_step(
                        "MODEL_CANDIDATE",
                        {
                            "message": encode_message(AIMessage(content=protocol_issue)),
                            "native_response_rejected": True,
                            "model_response": encode_message(response),
                        },
                        self.cp,
                    )
                    continue
                self.cp["steps"] += 1
                self.cp["pending_calls"] = response.tool_calls
                self.cp["batch_error"] = batch_error(response.tool_calls)
                self.cp["repair"] = not bool(response.tool_calls)
                self.backend.save_step(
                    "TOOL_CALL" if response.tool_calls else "MODEL_CANDIDATE",
                    {
                        "message": encode_message(response),
                        **(
                            {"tool_call_origin": "STRUCTURED_FINISH", "model_response": raw_finish}
                            if raw_finish is not None
                            else {}
                        ),
                    },
                    self.cp,
                )
            except AgentBudgetExceeded as exc:
                return self.backend.stop(str(exc), self.cp)


def cancelled_tool_result(call, reason):
    return ToolMessage(
        content='{"ok": false, "error": "' + reason + '"}',
        tool_call_id=call["id"],
        name=call["name"],
    )
