"""Open-ended dialogue, with an independent evidence review before publication."""

import hashlib
import json
import re
from typing import Annotated, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.agent.claim_safety import find_unsafe_claims
from app.agent.decision import DialogueAct, DialogueActKind, DialogueSubject, NegotiationDecision
from app.agent.negotiation_memory import MemoryDelta
from app.agent.reply_policy import DialogueReply


class ConversationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["INQUIRY", "REJECT"]
    reason: str = Field(min_length=1, max_length=500)
    reply: str = Field(min_length=1, max_length=800)
    needs_clarification: bool = False
    topics: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=8
    )


class ConversationFinish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: ConversationDecision
    stop_reason: str = Field(default="COMPLETED", max_length=80)
    memory_delta: MemoryDelta = Field(default_factory=MemoryDelta)

    def validate_non_write(self):
        # The schema itself restricts this envelope to non-write actions.
        pass

    def business_decision(self):
        value = self.decision.model_dump(exclude={"topics", "needs_clarification"})
        value["dialogue_acts"] = (
            [DialogueAct(kind=DialogueActKind.GENERAL, subject=DialogueSubject.GENERAL)]
            if self.decision.action == "INQUIRY"
            else []
        )
        return NegotiationDecision.model_validate(value)


class FactCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    claim: str = Field(min_length=1, max_length=300)
    evidence_path: str = Field(max_length=200)
    evidence_quote: str = Field(max_length=500)


class ReplyReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    approved: StrictBool
    reason: str = Field(max_length=600)
    fact_checks: list[FactCheck] = Field(max_length=20)


def validate_review_evidence(verdict, facts):
    if not verdict.approved:
        return
    for check in verdict.fact_checks:
        if not check.evidence_path or not check.evidence_quote:
            raise ValueError("批准回复时，事实引用不能为空")
        value = facts
        for part in check.evidence_path.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                raise ValueError("审核引用的事实路径不存在：" + check.evidence_path)
        if isinstance(value, (dict, list)) or value is None:
            raise ValueError("审核依据必须引用具体事实字段")
        original = str(value).lower() if isinstance(value, bool) else str(value)
        quote = check.evidence_quote.lower() if isinstance(value, bool) else check.evidence_quote
        if quote not in original:
            raise ValueError("审核引用与事实字段不一致：" + check.evidence_path)


def conversational(config):
    # Missing version denotes a historical frozen task, whose contract stays intact.
    return config.get("reply_strategy") == "conversation-v1"


def parse_finish(config, args):
    from app.agent.runtime_state import FinishTurnInput

    return (ConversationFinish if conversational(config) else FinishTurnInput).model_validate(args)


def review_input(backend, call):
    decision = ConversationFinish.model_validate(call["args"]).decision
    history = [
        {"seq": r.seq, "kind": r.kind, "content": r.payload.get("content", "")}
        for r in backend.records(backend.checkpoint["branch_id"])
        if r.visibility == "PUBLIC" and r.seq <= backend.current_input_seq
    ][-12:]
    payload = {
        # Auxiliary labels/reason/memory must not enable resampling a rejected reply.
        "finish": {"decision": {"action": decision.action, "reply": decision.reply}},
        "facts": backend.facts(),
        "conversation": history,
        "current_input_seq": backend.current_input_seq,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    key = hashlib.sha256(canonical.encode()).hexdigest()
    return key, payload


def review_messages(config, payload):
    catalog = []

    def visit(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}.{index}")
        elif value is not None:
            catalog.append({"path": path, "value": value})

    visit(payload["facts"], "")
    return [
        SystemMessage(content=config["reply_review_prompt"]["text"]),
        HumanMessage(
            content=json.dumps(
                {**payload, "fact_catalog": catalog},
                ensure_ascii=False,
                default=str,
            )
        ),
    ]


def preliminary_error(finish):
    # No seller-contact/camera tool exists in this toolset. A future promise to
    # contact the seller is a capability claim even when no deal is promised.
    if re.search(
        r"(?:我|我们)(?:会|可以|将|来|能|帮|替|已|已经).{0,14}"
        r"(?:(?:向|跟|与|联系)卖家.{0,8}(?:反馈|核实|确认|沟通|联系)|(?:通知|联系)(?:了)?卖家)",
        finish.decision.reply,
    ):
        return "当前没有联系卖家的工具，不能承诺向卖家反馈、联系或核实；可说明需要买家向卖家确认。"
    if any(
        term in finish.decision.reply.casefold()
        for term in (
            "自动接受阈值",
            "自动接受门槛",
            "系统提示词",
            "system prompt",
            "内部工具",
        )
    ):
        return "公开回复包含内部机制术语，请用自然语言说明限制，不展示内部实现。"
    violations = find_unsafe_claims(finish.decision.reply)
    return "未经授权或私密信息声明：" + ",".join(sorted(violations)) if violations else None


def reviewed_reply(backend, call, checkpoint):
    if not conversational(backend.config):
        return None
    key, _ = review_input(backend, call)
    stored = checkpoint.get("reply_reviews", {}).get(key)
    if not stored or stored.get("approved") is not True:
        raise ValueError("Reply lacks an approved review bound to current evidence and text")
    verdict = ReplyReview.model_validate(stored)
    validate_review_evidence(verdict, backend.facts())
    finish = ConversationFinish.model_validate(call["args"])
    return DialogueReply(finish.decision.reply, finish.decision.needs_clarification)
