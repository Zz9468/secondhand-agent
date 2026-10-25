"""Frozen references, conservative normalization and deterministic serial intent planning."""

import re
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from app.db.models import Message, NegotiationSession, Offer, Product
from app.db.models.graph import DisplayAck, PublicInteractionContext
from app.db.models.semantic import OfferDraft, SemanticOfferBasis, SemanticTurn, VerificationItem
from app.runtime.contracts import Conflict, TypedInput
from app.runtime.input_queue import lock_owner
from app.semantic.contracts import Candidate, ChatInput, IntentFrame
from app.semantic.evidence import (
    assess,
    clarification,
    public_answer,
    public_refusal,
    quote_in_transaction,
)

NEGATIVE_BATTERY_FLOOR = re.compile(
    r"电池[^，。；！？!?]{0,30}?(?:不到|低于|少于|小于)\s*(\d+(?:\.\d+)?)\s*%"
    r"[^，。；！？!?]{0,12}?(?:不买|不接受|不考虑|不成交)"
)


def usable_draft(draft, session, now, version):
    return bool(
        draft
        and draft.session_id == session.id
        and draft.version == version
        and draft.status in {"PENDING", "READY"}
        and draft.expires_at > now
        and draft.business_version == session.version
    )


def validate_references(db, session, payload, now):
    if payload.reply_to_message_id:
        message = db.get(Message, payload.reply_to_message_id)
        if not message or message.session_id != session.id:
            raise Conflict("引用消息不属于当前会话")
    if payload.draft_id:
        draft = db.get(OfferDraft, payload.draft_id)
        if not usable_draft(draft, session, now, payload.draft_version):
            raise Conflict("草案已失效或无权访问")


def frozen_context(db, run, session, now):
    payload = ChatInput.model_validate(run.payload)
    ack = db.get(DisplayAck, payload.display_receipt_id) if payload.display_receipt_id else None
    context = db.get(PublicInteractionContext, ack.context_id) if ack else None
    if not context:
        return None
    if (
        context.session_id != session.id
        or context.buyer_id != session.buyer_id
        or ack.buyer_id != session.buyer_id
        or ack.client_view_id != payload.client_view_id
        or ack.displayed_at > run.received_at
        or context.public_seq > run.seen_public_message_seq
        or context.expires_at <= now
        or context.business_version != session.version
    ):
        return None
    return context


def answer_question(db, run, session, product, topic, source_quote, now):
    """Answer public questions against the input's actual display, never the latest offer."""
    if topic == "approval_status":
        from app.services.approval_lifecycle import public_approval_status

        return public_approval_status(db, session, now)
    if topic in {
        "price",
        "listing_price",
        "listed_price",
        "current_price",
        "offer_price",
    } and not re.search(r"标价|页面|挂牌|公开价格", source_quote):
        context = frozen_context(db, run, session, now)
        offer = db.get(Offer, context.offer_id) if context and context.offer_id else None
        if (
            offer
            and offer.session_id == session.id
            and session.current_offer_id == offer.id
            and offer.expires_at > now
            and offer.status.value in {"ACCEPTED", "PROPOSED"}
        ):
            pending = (
                "该报价已接受，但仍等待你独立确认，尚未记录最终成交意向。"
                if offer.status.value == "ACCEPTED"
                else "该报价尚待卖家审批，尚未接受或确认成交意向。"
            )
            return (
                "ANSWERED",
                (
                    f"你已查看的当前报价为{offer.price}元；页面公开标价为{product.listed_price}元。"
                    + pending
                    + "询问价格本身不会触发确认。"
                ),
                None,
            )
    return public_answer(db, product, topic, source_quote)


def bound_draft(db, run, session, now):
    payload = ChatInput.model_validate(run.payload)
    if payload.draft_id:
        draft = db.get(OfferDraft, payload.draft_id)
        return draft if usable_draft(draft, session, now, payload.draft_version) else None
    context = frozen_context(db, run, session, now)
    refs = context.draft_refs if context else None
    if not refs:
        return None
    if len(refs) == 1:
        ref = refs[0]
    else:
        choice = re.fullmatch(r"(?:选|选择)(?:第)?([一二12])(?:个|项|方案)?[。！]?", payload.text)
        if not choice:
            return None
        index = 0 if choice[1] in {"一", "1"} else 1
        if index >= len(refs):
            return None
        ref = refs[index]
    draft = db.get(OfferDraft, ref["id"])
    return draft if usable_draft(draft, session, now, ref["version"]) else None


def parser_context(db, run, session, now):
    """Only public fields of the input's frozen context; no latest-message lookup."""
    draft = bound_draft(db, run, session, now)
    context = frozen_context(db, run, session, now)
    return {
        "has_bound_draft": draft is not None,
        "missing_fields": draft.missing if draft else [],
        "has_displayed_offer": bool(context and context.offer_id),
    }


def merge_candidate(original, patch):
    """A continuation cannot erase any existing predicate or unsupported commitment."""
    base = original.model_dump(mode="json")
    changes = patch.model_dump(mode="json")
    for key in ("amount", "delivery", "payer", "region"):
        if changes[key] is not None:
            base[key] = changes[key]
    for key in ("conditions", "commitments"):
        base[key] += [item for item in changes[key] if item not in base[key]]
    if patch.delivery == "pickup" and patch.payer is None:
        base["payer"] = "buyer"
    return Candidate.model_validate(base)


def condition_scope(intent, actions, text):
    """Narrow only an unambiguous local alternative; shared/unclear scope stays conservative."""
    parts = re.split(r"或者|或是", text)
    if (
        len(parts) == len(actions) > 1
        and all(i.alternative_group == intent.alternative_group for i in actions)
        and intent.alternative_group
        and not re.search(r"如果|只要|前提|的话", parts[0])
        and not NEGATIVE_BATTERY_FLOOR.search(parts[0])
        and not re.search(r"都|均|共同|两个方案|两种方案", text)
    ):
        matching = [part for part in parts if intent.source_quote in part]
        if len(matching) == 1:
            return matching[0]
        # Some valid model quotes cover the entire alternatives sentence. A unique explicit
        # amount can still locate the branch; repeated amounts remain conservative.
        if intent.candidate and intent.candidate.amount is not None:
            matching = [
                part
                for part in parts
                if intent.candidate.amount
                in {Decimal(n) for n in re.findall(r"\d+(?:\.\d+)?", part)}
            ]
            if len(matching) == 1:
                return matching[0]
    return text


def safe_frame(frame, text):
    ordered = frame.validated_for(text)
    actions = [
        i
        for i in ordered
        if i.type
        in {
            "PROPOSE_OFFER",
            "CHANGE_TERMS",
            "CONFIRM_INTENT",
            "WITHDRAW_OFFER",
            "CLOSE_SESSION",
        }
    ]
    candidates = [i.candidate for i in ordered if i.candidate]
    if frame.ambiguous:
        return ordered, False
    for item in ordered:
        candidate = item.candidate
        if candidate is None:
            continue
        scope = condition_scope(item, actions, text)
        # A negative purchase condition reverses the comparison in the quoted fragment.
        # Validate against the whole branch, not only the model's potentially truncated quote.
        negative_floor = NEGATIVE_BATTERY_FLOOR.search(scope)
        if negative_floor and not any(
            p.fact == "battery_health_pct"
            and p.operator == ">="
            and re.fullmatch(r"\d+(?:\.\d+)?", p.value)
            and Decimal(p.value) == Decimal(negative_floor[1])
            for p in candidate.conditions
        ):
            return ordered, False
        if re.search(r"如果|只要|前提|的话", scope):
            if not candidate.conditions and not candidate.commitments:
                return ordered, False
            if re.search(r"没修|未修|无维修|没有维修", scope) and not any(
                p.fact == "repair_history" and p.operator == "==" and p.value == "NO"
                for p in candidate.conditions
            ):
                return ordered, False
            threshold = re.search(
                r"电池.*?(至少|不低于|>=|不超过|至多|<=|等于)\s*(\d+(?:\.\d+)?)\s*%", scope
            )
            if threshold:
                operator = (
                    ">="
                    if threshold[1] in {"至少", "不低于", ">="}
                    else "<="
                    if threshold[1] in {"不超过", "至多", "<="}
                    else "=="
                )
                if not any(
                    p.fact == "battery_health_pct"
                    and p.operator == operator
                    and p.value == threshold[2]
                    for p in candidate.conditions
                ):
                    return ordered, False
    # These vetoes supplement calibrated model semantics; they never create an action.
    if (
        re.search(r"(?:如果|只要|前提|的话)", text)
        and candidates
        and not any(c.conditions or c.commitments for c in candidates)
    ):
        return ordered, False
    if re.search(r"(?:或者|二选一)", text) and len(actions) < 2:
        return ordered, False
    if re.search(r"(?:先看看|不报价|只是预算|只是比价)", text) and candidates:
        return ordered, False
    if "预算" in text and not re.search(r"可以吗|行吗|报价|我出", text) and candidates:
        return ordered, False
    if re.search(r"不(?:接受|考虑|同意|想要|愿意).*(?:自提|包邮|报价)", text) and actions:
        return ordered, False
    for item in actions:
        if item.type == "CONFIRM_INTENT" and re.search(
            r"不行|不要|不想|不接受|不同意|不确认|别确认|勿确认|不能|拒绝", item.source_quote
        ):
            return ordered, False
        if item.type == "CLOSE_SESSION" and (
            not re.search(r"结束|关闭|终止|不买", item.source_quote)
            or re.search(
                r"(?:不要|不用|不想|不必|别|不能|不是要).{0,2}(?:结束|关闭|终止)"
                r"|不(?:结束|关闭|终止)|(?:不是|没说)不买",
                item.source_quote,
            )
            or re.search(r"(?:如果|假如|若|等到|后续).{0,100}(?:结束|关闭|终止|不买)", text)
        ):
            return ordered, False
        if item.type == "WITHDRAW_OFFER" and (
            not re.search(r"撤回|撤销|收回|取消.{0,8}(?:报价|提议|方案)", item.source_quote)
            or re.search(
                r"(?:不|不要|不用|不想|不必|别|不能|不可以|不需要|没有要求|不是).{0,4}"
                r"(?:撤回|撤销|收回|取消)",
                item.source_quote,
            )
        ):
            return ordered, False
    if (
        re.search(r"(?:今天.*发货|保修|保真|赠送)", text)
        and candidates
        and not any(c.commitments for c in candidates)
    ):
        return ordered, False
    correction = re.search(r"不是\s*(\d+(?:\.\d+)?)", text)
    if correction and any(c.amount == Decimal(correction[1]) for c in candidates):
        return ordered, False
    if any(i.type == "CONFIRM_INTENT" for i in actions) and re.search(
        r"但是|不过|改成|得包邮", text
    ):
        return ordered, False
    return ordered, True


def plan_turn(factory, run_id, owner, frame_value, now):
    with factory() as db, db.begin():
        run, _ = lock_owner(db, run_id, owner, now)
        session = db.get(NegotiationSession, run.session_id, with_for_update=True)
        old = db.get(SemanticTurn, run_id)
        if old:
            return
        product = db.get(Product, session.product_id, with_for_update=True)
        payload = ChatInput.model_validate(run.payload)
        frame = IntentFrame.model_validate(frame_value)
        ordered, safe = safe_frame(frame, payload.text)
        results, drafts = [], []
        command = TypedInput(
            kind="QUERY",
            text=payload.text,
            client_view_id=payload.client_view_id,
            display_receipt_id=payload.display_receipt_id,
        )
        actions = [
            i
            for i in ordered
            if i.type
            in {
                "PROPOSE_OFFER",
                "CHANGE_TERMS",
                "CONFIRM_INTENT",
                "WITHDRAW_OFFER",
                "CLOSE_SESSION",
            }
        ]
        changes = [
            i
            for i in ordered
            if i.type == "PROVIDE_INFO"
            and i.topic not in {"budget", "comparison"}
            and bound_draft(db, run, session, now)
        ]
        alternatives = len(actions) > 1 and all(
            i.type in {"PROPOSE_OFFER", "CHANGE_TERMS"}
            and i.alternative_group
            and i.alternative_group == actions[0].alternative_group
            for i in actions
        )
        conflict = len(actions) + len(changes) > 1 and not (alternatives and not changes)
        selected = None
        statuses = {}
        for intent in ordered:
            status, answer, evidence = "CLARIFY", "请明确本次希望执行的操作。", None
            if intent.type in {"ASK_PRODUCT", "ASK_TERMS"}:
                status, answer, evidence = answer_question(
                    db, run, session, product, intent.topic, intent.source_quote, now
                )
            elif not safe or conflict:
                answer = "输入有冲突或条件不完整，请明确要保留的方案和条件。"
                requirements = [
                    word
                    for word in ("发票", "维修", "电池", "保修", "原装", "运费")
                    if word in payload.text
                ]
                if requirements:
                    answer = (
                        "、".join(requirements)
                        + "等必要条件尚未完整核实，未提交报价。请明确要保留的方案及条件。"
                    )
            elif intent.type in {"PROPOSE_OFFER", "CHANGE_TERMS"} or intent in changes:
                bound = bound_draft(db, run, session, now)
                candidate = intent.candidate or Candidate()
                continuation = (
                    intent.type in {"PROVIDE_INFO", "CHANGE_TERMS"} or candidate.amount is None
                )
                context = frozen_context(db, run, session, now)
                original = None
                if bound and continuation:
                    original = Candidate.model_validate(bound.candidate)
                elif intent.type == "CHANGE_TERMS" and context and context.offer_id:
                    basis = db.get(SemanticOfferBasis, context.offer_id)
                    if basis:
                        original = Candidate.model_validate(basis.candidate)
                if original:
                    candidate = merge_candidate(original, candidate)
                elif continuation and candidate.amount is None:
                    missing, _, _, _ = assess(db, product, candidate, now)
                    answer = clarification(candidate, missing) + (
                        "无法绑定唯一有效且已展示的草案，请引用方案或完整重述报价。"
                    )
                    results.append({"id": intent.id, "status": status, "text": answer})
                    statuses[intent.id] = status
                    continue
                missing, _, _, _ = assess(db, product, candidate, now)
                if any(statuses.get(dep) not in {"ANSWERED", "READY"} for dep in intent.depends_on):
                    missing.append("dependency_unresolved")
                if bound and continuation:
                    draft = bound
                    draft.version += 1
                    draft.source_messages = [*draft.source_messages, run.buyer_message_id]
                else:
                    draft = OfferDraft(
                        id=uuid4().hex,
                        session_id=session.id,
                        input_id=run.id,
                        version=1,
                        source_messages=[run.buyer_message_id],
                        business_version=session.version,
                        related_offer_id=session.current_offer_id,
                        alternative_group=f"{run.id}:{intent.alternative_group}"
                        if alternatives
                        else None,
                        expires_at=now + timedelta(hours=24),
                    )
                    db.add(draft)
                draft.candidate = candidate.model_dump(mode="json")
                draft.missing = missing
                draft.status = "PENDING" if missing else "READY"
                db.flush()
                drafts.append(draft.id)
                if any(
                    m.startswith(("condition:", "fact:", "cost_", "delivery_not")) for m in missing
                ):
                    verification = db.scalar(
                        select(VerificationItem).where(VerificationItem.draft_id == draft.id)
                    )
                    if not verification:
                        verification = VerificationItem(
                            id=uuid4().hex, draft_id=draft.id, product_id=product.id
                        )
                        db.add(verification)
                    verification.status, verification.reasons = "OPEN", missing
                if alternatives:
                    status, answer = "CHOICE_REQUIRED", "已保留此备选及原条件，请选择一个方案。"
                    if missing:
                        answer += clarification(candidate, missing)
                elif missing:
                    status = "WAITING_INFO"
                    answer = clarification(candidate, missing)
                elif len(actions) + len(changes) <= 1:
                    quote, terms, _ = quote_in_transaction(db, product, candidate, now)
                    command = command.model_copy(
                        update={
                            "kind": "REPLACE" if session.current_offer_id else "SUBMIT",
                            "terms": terms,
                            "offer_id": session.current_offer_id,
                        }
                    )
                    selected = draft.id
                    status, answer = "READY", "报价条款完整，等待业务校验。"
                else:
                    answer = "本次包含多个变更，请选择一个方案。"
            elif intent.type == "CONFIRM_INTENT":
                context = frozen_context(db, run, session, now)
                if (
                    context
                    and context.offer_id
                    and (not payload.offer_id or payload.offer_id == context.offer_id)
                ):
                    command = command.model_copy(
                        update={"kind": "CONFIRM", "offer_id": context.offer_id}
                    )
                    status, answer = "READY", "正在核验已展示报价的最终确认。"
                else:
                    answer = "请先查看并确认一张仍有效的报价卡片。"
            elif intent.type == "WITHDRAW_OFFER" and session.current_offer_id:
                context = frozen_context(db, run, session, now)
                amounts = {Decimal(n) for n in re.findall(r"\d+(?:\.\d+)?", intent.source_quote)}
                current = db.get(Offer, session.current_offer_id)
                if (
                    (payload.display_receipt_id and context is None)
                    or (context and context.offer_id and context.offer_id != current.id)
                    or (amounts and amounts != {current.price})
                ):
                    answer = "撤回目标与当前报价不一致，请明确引用当前方案。"
                else:
                    command = command.model_copy(
                        update={"kind": "WITHDRAW", "offer_id": session.current_offer_id}
                    )
                    status, answer = "READY", "正在撤回当前报价。"
            elif intent.type == "CLOSE_SESSION":
                command = command.model_copy(update={"kind": "CLOSE"})
                status, answer = "READY", "正在结束协商。"
            elif intent.type in {"PROVIDE_INFO", "OTHER"}:
                status, answer = "RECORDED", "已记录这条信息，本条未作为正式报价。"
                if (
                    intent.type == "PROVIDE_INFO"
                    and intent.topic not in {"budget", "comparison"}
                    and (
                        (intent.candidate and intent.candidate.amount is None)
                        or payload.text in {"继续", "自提", "快递", "选第一个", "选第二个"}
                    )
                ):
                    status, answer = "CLARIFY", "无法绑定唯一有效草案，请引用方案或完整重述报价。"
            statuses[intent.id] = status
            results.append(
                {"id": intent.id, "status": status, "text": answer, "fact_version": evidence}
            )
        refusal = public_refusal(payload.text)
        if refusal:
            results.append({"id": "public-boundary", "status": "REFUSED", "text": refusal})
        db.add(
            SemanticTurn(
                input_id=run.id,
                frame={
                    "parsed": frame_value,
                    "drafts": [
                        {"id": d.id, "version": d.version, "candidate": d.candidate}
                        for d in (db.get(OfferDraft, key) for key in drafts)
                    ],
                },
                command=command.model_dump(mode="json"),
                results=results,
                draft_ids=drafts,
                selected_draft_id=selected,
            )
        )


def load_command(db, run):
    if run.payload["kind"] == "CHAT":
        turn = db.get(SemanticTurn, run.id)
        if not turn:
            raise Conflict("缺少已验证的语义计划")
        return TypedInput.model_validate(turn.command)
    return TypedInput.model_validate(run.payload)
