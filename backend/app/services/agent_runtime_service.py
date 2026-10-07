import hashlib
import json
import time
from copy import deepcopy
from datetime import UTC, datetime

from langchain_core.messages import ToolMessage
from sqlalchemy import func, or_, select

from app.agent.conversation_reply import parse_finish, reviewed_reply
from app.agent.decision_provider import native_tool_choice, response_format_contract
from app.agent.message_protocol import (
    StreamRecord,
    decode_message,
    encode_message,
    json_value,
    wire_message,
)
from app.agent.model_observation import ProviderUsage
from app.agent.negotiation_memory import MemoryUpdater, record_memory_failure
from app.agent.reply_policy import FormalReplyRenderer
from app.agent.runtime_state import call_budget, call_model_parameters
from app.agent.seller_agent import AgentTurnOutcome, AgentTurnPreparation, AgentTurnResult
from app.agent.tools import AgentToolContext, build_seller_tools
from app.agent.tools.gateway import search_history, tool_schemas, visible_tool_result
from app.core.config import get_settings
from app.db.models import (
    AgentCallSnapshot,
    AgentMessage,
    ModelExecutionTask,
    ModelTaskStatus,
    NegotiationSession,
)
from app.observability.service import ObservabilityEventInput
from app.services.agent_message_service import AgentMessageService
from app.services.approval_service import ApprovalService
from app.services.negotiation_service import NegotiationService
from app.services.product_service import ProductService


def initial_context(db, session_id):
    context = AgentMessageService.context(db, session_id)
    artifact = (
        db.get(AgentMessage, context.summary_artifact_id) if context.summary_artifact_id else None
    )
    return {
        "summary": context.summary_text or "",
        "covered_through": context.summary_covered_through_seq,
        "summary_sources": artifact.payload.get("source_seqs", []) if artifact else [],
        "memory": deepcopy(context.memory_json) or {},
        "context_version": context.context_version,
        "summary_artifact_id": context.summary_artifact_id,
        "memory_artifact_id": context.memory_artifact_id,
        "memory_covered_through": context.memory_covered_through_seq,
    }


class ProductionRuntimeBackend:
    def __init__(self, chat, pending, leased, provider):
        self.chat, self.pending, self.leased, self.provider = chat, pending, leased, provider
        self.factory = chat._session_factory
        self.task_service = chat._task_service
        self.config = leased.input_snapshot["agent_config"]
        self.current_input_seq = leased.input_snapshot["current_input_seq"]
        self.high_water = leased.input_snapshot["input_high_water_seq"]
        self.initial_context = leased.input_snapshot["initial_context"]
        self.checkpoint = deepcopy(leased.runtime_checkpoint) or {}
        self.request = chat._deserialize_decision_request(leased.input_snapshot)
        self.lease_token = leased.lease_token
        self.stale_reason = None
        with self.factory() as db:
            negotiation = db.get(NegotiationSession, leased.session_id)
            self.buyer_id = chat._required_buyer_id(negotiation)
        self.tool_schemas = self.config.get("tools") or tool_schemas(
            self._tools(self.factory), self.config
        )

    def _tools(self, factory):
        return build_seller_tools(
            context=AgentToolContext(
                self.leased.session_id, self.buyer_id, self.request.current_turn_offer_id
            ),
            product_service=ProductService(factory),
            negotiation_service=NegotiationService(factory),
            approval_service=ApprovalService(factory),
        )

    def _active(self, db):
        return self.task_service.require_active_lease_in_transaction(
            db=db,
            task_id=self.leased.id,
            lease_token=self.lease_token,
        )

    def ensure_current(self):
        with self.factory() as db, db.begin():
            task = self.task_service.renew_lease_in_transaction(
                db=db,
                task_id=self.leased.id,
                lease_token=self.lease_token,
                seconds=max(
                    get_settings().model_task_lease_seconds,
                    int(self.config["model"]["timeout"]) + 10,
                ),
            )
            negotiation = db.get(NegotiationSession, task.session_id, with_for_update=True)
            self.stale_reason = self.chat._stale_reason(db, task=task, negotiation=negotiation)
        return self.stale_reason is None

    def records(self, branch_id):
        with self.factory() as db:
            completed = select(ModelExecutionTask.id).where(
                ModelExecutionTask.status == ModelTaskStatus.SUCCEEDED
            )
            rows = db.scalars(
                select(AgentMessage)
                .where(
                    AgentMessage.session_id == self.leased.session_id,
                    or_(
                        (AgentMessage.stream_seq <= self.high_water)
                        & (
                            (AgentMessage.visibility == "PUBLIC")
                            | AgentMessage.model_task_id.in_(completed)
                        ),
                        (AgentMessage.model_task_id == self.leased.id)
                        & (AgentMessage.run_branch_id == branch_id),
                    ),
                )
                .order_by(AgentMessage.stream_seq)
            )
            return [StreamRecord.from_row(r) for r in rows]

    def facts(self):
        # ensure_current checks current business versions immediately before this input.
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

    def save_checkpoint(self, checkpoint):
        with self.factory() as db, db.begin():
            self.task_service.update_checkpoint_in_transaction(
                db=db,
                task_id=self.leased.id,
                lease_token=self.lease_token,
                checkpoint=checkpoint,
            )

    def save_step(self, kind, payload, checkpoint):
        with self.factory() as db, db.begin():
            self._active(db)
            AgentMessageService.append(
                db,
                session_id=self.leased.session_id,
                task_id=self.leased.id,
                branch_id=checkpoint["branch_id"],
                step_id=f"model:{checkpoint['steps']}",
                lease_attempt=self.leased.attempt_count,
                event_key=(
                    f"task:{self.leased.id}:{checkpoint['branch_id']}:model:{checkpoint['steps']}"
                ),
                role="ASSISTANT",
                kind=kind,
                payload=payload,
            )
            self.task_service.update_checkpoint_in_transaction(
                db=db,
                task_id=self.leased.id,
                lease_token=self.lease_token,
                checkpoint=checkpoint,
            )

    def cached_response(self, purpose, checkpoint):
        if purpose not in {
            "REACT",
            "REACT_ENDING",
            "REACT_FINISH",
            "CONVERSATION_FINISH",
            "REPLY_REVIEW",
        }:
            return None
        step = (
            checkpoint["reply_review_key"]
            if purpose == "REPLY_REVIEW"
            else f"{purpose}:{checkpoint['steps'] + 1}"
        )
        with self.factory() as db:
            row = db.scalar(
                select(AgentCallSnapshot)
                .where(
                    AgentCallSnapshot.model_task_id == self.leased.id,
                    AgentCallSnapshot.run_branch_id == checkpoint["branch_id"],
                    AgentCallSnapshot.logical_step_id == step,
                    AgentCallSnapshot.purpose == purpose,
                    AgentCallSnapshot.status == "SUCCEEDED",
                )
                .order_by(AgentCallSnapshot.id.desc())
            )
            return decode_message(row.response_payload) if row else None

    def call_model(self, messages, tools, purpose, manifest, checkpoint):
        step = (
            f"{purpose}:{checkpoint['steps'] + 1}"
            if purpose in {"REACT", "REACT_ENDING", "REACT_FINISH", "CONVERSATION_FINISH"}
            else checkpoint["reply_review_key"]
            if purpose == "REPLY_REVIEW"
            else f"{purpose}:{checkpoint['maintenance_calls']}"
        )
        payload = json_value(
            {
                "messages": [encode_message(m) for m in messages],
                "provider_messages": [wire_message(m) for m in messages],
                "tools": tools,
                "tool_choice": native_tool_choice(purpose) if tools else None,
                "parallel_tool_calls": False if tools else None,
                "response_format": response_format_contract(purpose),
                "model": call_model_parameters(self.config, purpose),
                "max_output_tokens": call_budget(self.config, purpose)["output_tokens"],
            }
        )
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.factory() as db, db.begin():
            self._active(db)
            # A prior STARTED call has unknown completion after process interruption.
            for old in db.scalars(
                select(AgentCallSnapshot).where(
                    AgentCallSnapshot.model_task_id == self.leased.id,
                    AgentCallSnapshot.status == "STARTED",
                )
            ):
                old.status = "UNKNOWN"
                old.completed_at = datetime.now(UTC).replace(tzinfo=None)
            attempt = (
                db.scalar(
                    select(func.max(AgentCallSnapshot.call_attempt)).where(
                        AgentCallSnapshot.model_task_id == self.leased.id,
                        AgentCallSnapshot.logical_step_id == step,
                    )
                )
                or 0
            ) + 1
            row = AgentCallSnapshot(
                model_task_id=self.leased.id,
                run_branch_id=checkpoint["branch_id"],
                logical_step_id=step,
                call_attempt=attempt,
                purpose=purpose,
                request_payload=payload,
                request_hash=hashlib.sha256(canonical.encode()).hexdigest(),
                context_manifest=json_value(
                    {
                        **manifest,
                        "business_session_version": self.leased.session_version,
                        "policy_version": self.leased.policy_version,
                        "initial_context": self.initial_context,
                        "budget": self.config["budget"],
                    }
                ),
                status="STARTED",
            )
            db.add(row)
            db.flush()
            call_id = row.id
        started = time.perf_counter()
        try:
            response = self.provider.invoke_agent(messages, tools, self.config, purpose=purpose)
        except Exception as exc:
            try:
                with self.factory() as db, db.begin():
                    self._active(db)
                    row = db.get(AgentCallSnapshot, call_id)
                    row.status = "UNKNOWN"
                    row.completed_at = datetime.now(UTC).replace(tzinfo=None)
            finally:
                self._observe_call(purpose, started, error=exc)
            raise
        usage = response.usage_metadata or {}
        with self.factory() as db, db.begin():
            self._active(db)
            row = db.get(AgentCallSnapshot, call_id)
            row.response_payload = encode_message(response)
            row.status = "SUCCEEDED"
            row.usage_json = json_value(usage)
            row.completed_at = datetime.now(UTC).replace(tzinfo=None)
        self._observe_call(purpose, started, metadata=usage)
        return response

    def _observe_call(self, purpose, started, *, metadata=None, error=None):
        metadata = metadata or {}
        usage = self._priced_usage(
            ProviderUsage(
                provider=self.config["model"]["provider"],
                model_name=self.config["model"]["name"],
                input_tokens=metadata.get("input_tokens"),
                output_tokens=metadata.get("output_tokens"),
                total_tokens=metadata.get("total_tokens"),
                cached_input_tokens=(metadata.get("input_token_details") or {}).get("cache_read", 0)
                if metadata
                else None,
            )
        )
        self.chat._observer.record(
            ObservabilityEventInput(
                event_type="MODEL_CALL_COMPLETED",
                action="V4_MODEL_CALL",
                outcome="ERROR" if error else "SUCCESS",
                attempt_count=self.leased.attempt_count,
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_category=self.chat._retry_policy.classify(error).category.value
                if error
                else None,
                usage=usage,
                attributes={"model_call_purpose": purpose},
            )
        )

    def _usage(self, db):
        rows = list(
            db.scalars(
                select(AgentCallSnapshot).where(AgentCallSnapshot.model_task_id == self.leased.id)
            )
        )
        complete = bool(rows) and all(
            r.usage_json and {"input_tokens", "output_tokens"}.issubset(r.usage_json) for r in rows
        )

        usage = ProviderUsage(
            provider=self.config["model"]["provider"],
            model_name=self.config["model"]["name"],
            input_tokens=sum(r.usage_json["input_tokens"] for r in rows) if complete else None,
            output_tokens=sum(r.usage_json["output_tokens"] for r in rows) if complete else None,
            total_tokens=sum(r.usage_json.get("total_tokens", 0) for r in rows)
            if complete
            else None,
            cached_input_tokens=sum(
                r.usage_json.get("input_token_details", {}).get("cache_read", 0) for r in rows
            )
            if complete
            else None,
        )
        return self._priced_usage(usage)

    def _priced_usage(self, usage):
        from decimal import Decimal

        from app.services.model_usage_service import ModelUsageService

        prices = self.config["prices"]
        settings = get_settings().model_copy(
            update={
                "model_input_price_per_million": Decimal(prices["input"])
                if prices["input"] is not None
                else None,
                "model_output_price_per_million": Decimal(prices["output"])
                if prices["output"] is not None
                else None,
                "model_cached_input_price_per_million": Decimal(prices["cached"])
                if prices["cached"] is not None
                else None,
                "model_cost_currency": prices["currency"],
            }
        )
        return ModelUsageService(settings).build(usage)

    def _tool_result(self, db, call, value, checkpoint):
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
        source = AgentMessageService.append(
            db,
            session_id=self.leased.session_id,
            task_id=self.leased.id,
            branch_id=checkpoint["branch_id"],
            step_id=f"model:{checkpoint['steps']}",
            lease_attempt=self.leased.attempt_count,
            event_key=f"task:{self.leased.id}:tool:{call['id']}",
            role="TOOL",
            kind="TOOL_RESULT",
            payload={"message": encode_message(message)},
            tool_call_id=call["id"],
        )
        if self.config["memory_enabled"]:
            checkpoint["memory"] = MemoryUpdater.record_tool(
                checkpoint.get("memory", self.initial_context["memory"]),
                StreamRecord.from_row(source),
                call,
                value,
            )
        checkpoint["pending_calls"] = checkpoint["pending_calls"][1:]
        self.task_service.update_checkpoint_in_transaction(
            db=db,
            task_id=self.leased.id,
            lease_token=self.lease_token,
            checkpoint=checkpoint,
        )

    def _finish(self, db, result, checkpoint, *, memory=None, stop_reason="COMPLETED"):
        if memory is None and checkpoint.get("memory") is not None:
            memory = checkpoint["memory"]
        checkpoint.update(
            terminal_action_committed=result.is_formal_commitment,
            stop_reason=stop_reason,
            pending_calls=[],
        )
        turn = self.chat._persist_result(
            db,
            session_id=self.leased.session_id,
            buyer_snapshot=self.pending.buyer_snapshot,
            reply_request_id=self.leased.input_snapshot["reply_request_id"],
            result=result,
            idempotent_replay=self.pending.idempotent_replay,
            task_id=self.leased.id,
        )
        context = AgentMessageService.context(db, self.leased.session_id)
        expected = self.initial_context["context_version"]
        if context.context_version == expected and not self.stale_reason:
            if checkpoint.get("summary"):
                artifact = AgentMessageService.append(
                    db,
                    session_id=self.leased.session_id,
                    task_id=self.leased.id,
                    role="SYSTEM",
                    kind="CONTEXT_ARTIFACT",
                    event_key=f"task:{self.leased.id}:summary",
                    payload={
                        "type": "SUMMARY",
                        "text": checkpoint["summary"],
                        "covered_through": checkpoint["covered_through"],
                        "source_seqs": checkpoint.get("summary_sources", []),
                    },
                )
                context.summary_text = checkpoint["summary"]
                context.summary_covered_through_seq = checkpoint["covered_through"]
                context.summary_artifact_id = artifact.id
            if memory is not None and self.config["memory_enabled"]:
                artifact = AgentMessageService.append(
                    db,
                    session_id=self.leased.session_id,
                    task_id=self.leased.id,
                    role="SYSTEM",
                    kind="CONTEXT_ARTIFACT",
                    event_key=f"task:{self.leased.id}:memory",
                    payload={"type": "MEMORY", "memory": memory},
                )
                context.memory_json = memory
                context.memory_artifact_id = artifact.id
                context.memory_covered_through_seq = checkpoint.get(
                    "memory_covered_through", self.initial_context.get("memory_covered_through", 0)
                )
            context.context_version += 1
        self.task_service.update_checkpoint_in_transaction(
            db=db,
            task_id=self.leased.id,
            lease_token=self.lease_token,
            checkpoint=checkpoint,
        )
        if self.stale_reason:
            self.task_service.mark_stale_in_transaction(
                db=db,
                task_id=self.leased.id,
                lease_token=self.lease_token,
                error_message=self.stale_reason,
                usage=self._usage(db),
            )
        else:
            self.task_service.complete_success_in_transaction(
                db=db,
                task_id=self.leased.id,
                lease_token=self.lease_token,
                result_snapshot={"stop_reason": stop_reason, "runtime": "V4"},
                usage=self._usage(db),
            )
        return turn

    def execute(self, call, checkpoint, *, protocol_error=None):
        with self.factory() as db, db.begin():
            task = self._active(db)
            negotiation = db.get(NegotiationSession, task.session_id, with_for_update=True)
            self.stale_reason = self.chat._stale_reason(db, task=task, negotiation=negotiation)
            if self.stale_reason:
                return self._stop_in_transaction(db, "BUSINESS_STATE_CHANGED", checkpoint)
            error = protocol_error
            value = None
            terminal = None
            memory = None
            savepoint = db.begin_nested()
            try:
                factory = self.chat._transaction_session_factory(db)
                tools = {t.name: t for t in self._tools(factory)}
                name, args = call["name"], call["args"]
                if error:
                    raise ValueError(error)
                if name == "search_conversation_history":
                    value = search_history(self.records(checkpoint["branch_id"]), args)
                elif name == "finish_turn":
                    finish = parse_finish(self.config, args)
                    finish.validate_non_write()
                    dialogue_reply = reviewed_reply(self, call, checkpoint)
                    try:
                        memory = (
                            MemoryUpdater.apply(
                                checkpoint.get("memory", self.initial_context["memory"]),
                                finish.memory_delta,
                                self.records(checkpoint["branch_id"]),
                            )
                            if self.config["memory_enabled"]
                            else None
                        )
                        if not checkpoint.get("memory_refresh_incomplete"):
                            checkpoint["memory_covered_through"] = self.current_input_seq
                    except ValueError as exc:
                        record_memory_failure(
                            checkpoint,
                            "FINISH_DELTA",
                            exc,
                            [
                                seq
                                for entry in finish.memory_delta.entries
                                for seq in entry.source_seqs
                            ],
                        )
                        memory = None
                    agent = self.chat._build_agent(
                        session_id=task.session_id,
                        buyer_id=self.buyer_id,
                        session_factory=factory,
                        current_turn_offer_id=self.request.current_turn_offer_id,
                    )
                    terminal = agent.apply_prepared(
                        AgentTurnPreparation(decision_request=self.request),
                        decision=finish.business_decision(),
                        validated_dialogue_reply=dialogue_reply,
                    )
                    value = {
                        "ok": True,
                        "outcome": terminal.outcome.value,
                        "topics": getattr(finish.decision, "topics", []),
                        "reply_reviewed": dialogue_reply is not None,
                    }
                else:
                    if (
                        name in {"accept_offer", "request_approval"}
                        and args.get("offer_id") != self.request.current_turn_offer_id
                    ):
                        raise ValueError("Only the current formal buyer offer is authorized")
                    value = tools[name].invoke(args)
                    if value.get("ok") and name == "submit_counter_offer":
                        terminal = AgentTurnResult(
                            FormalReplyRenderer().render_counter_offer(value),
                            AgentTurnOutcome.COUNTER_OFFERED,
                            None,
                            value["offer"]["id"],
                        )
                    elif value.get("ok") and name == "accept_offer":
                        terminal = AgentTurnResult(
                            FormalReplyRenderer().render_accepted_offer(value),
                            AgentTurnOutcome.OFFER_ACCEPTED,
                            None,
                            value["offer"]["id"],
                        )
                    elif value.get("ok") and name == "request_approval":
                        if not self.chat._build_agent(
                            session_id=task.session_id,
                            buyer_id=self.buyer_id,
                            session_factory=factory,
                        )._is_valid_approval(value, offer_id=args["offer_id"]):
                            raise ValueError("Invalid committed approval result")
                        terminal = AgentTurnResult(
                            "已将这份报价提交卖家确认，目前还不能视为接受或成交，请等待后续结果。",
                            AgentTurnOutcome.NEEDS_SELLER_CONFIRMATION,
                            None,
                        )
                savepoint.commit()
            except (ValueError, KeyError, TypeError) as exc:
                savepoint.rollback()
                terminal = None
                memory = None
                value = {
                    "ok": False,
                    "error": {"code": "TOOL_INPUT_REJECTED", "message": str(exc)[:200]},
                }
            self._tool_result(db, call, json_value(value), checkpoint)
            if terminal:
                return self._finish(db, terminal, checkpoint, memory=memory)
        return None

    def _stop_in_transaction(self, db, reason, checkpoint):
        for call in list(checkpoint["pending_calls"]):
            self._tool_result(db, call, {"ok": False, "error": {"code": reason}}, checkpoint)
        result = AgentTurnResult(
            "当前条件暂时无法安全处理，请补充交易条件或稍后再试。",
            AgentTurnOutcome.SAFE_FAILURE,
            None,
        )
        return self._finish(db, result, checkpoint, stop_reason=reason)

    def stop(self, reason, checkpoint):
        with self.factory() as db, db.begin():
            self._active(db)
            return self._stop_in_transaction(db, reason, checkpoint)
