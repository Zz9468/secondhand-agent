"""Typed seller evidence and deterministic evaluation; never infer costs from chat."""

import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import select

from app.db.models.semantic import CostQuote, ProductFactValue, SemanticOfferBasis, ShippingCostRule
from app.runtime.contracts import Conflict, Terms
from app.semantic.contracts import Candidate

FACTS = {
    "battery_health_pct": ("decimal", "percent"),
    "repair_history": ("enum", "none"),
    "allowed_delivery_methods": ("set", "none"),
}


def normalize_fact(attribute, value):
    if attribute not in FACTS:
        raise Conflict("不支持的商品属性")
    if attribute == "battery_health_pct":
        if value is not None:
            try:
                number = Decimal(str(value))
                if not number.is_finite() or not 0 <= number <= 100 or number % Decimal("0.01"):
                    raise ValueError()
                value = f"{number:.2f}"
            except (InvalidOperation, ValueError):
                raise Conflict("电池健康须为 0 到 100 的百分数或未知") from None
    elif attribute == "repair_history":
        if value not in ("YES", "NO", "UNKNOWN"):
            raise Conflict("维修记录须为 YES / NO / UNKNOWN")
    elif (
        not isinstance(value, list)
        or not value
        or any(item not in ("shipping", "pickup") for item in value)
        or len(set(value)) != len(value)
    ):
        raise Conflict("配送方式须为非空且不重复的 shipping / pickup 集合")
    kind, unit = FACTS[attribute]
    return {"type": kind, "unit": unit, "value": value}


def facts_for(db, product_id):
    rows = db.scalars(
        select(ProductFactValue)
        .where(ProductFactValue.product_id == product_id)
        .order_by(ProductFactValue.version)
    )
    return {row.attribute: row for row in rows}


def evaluate(predicate, facts, index):
    fact = facts.get(predicate.fact)
    result = "UNKNOWN"
    value = fact.value["value"] if fact else None
    try:
        if predicate.fact == "battery_health_pct" and predicate.operator in {">=", "<=", "=="}:
            expected = Decimal(predicate.value)
            if not expected.is_finite() or not 0 <= expected <= 100:
                raise ValueError()
            if value is not None:
                actual = Decimal(value)
                result = (
                    "SATISFIED"
                    if {
                        ">=": actual >= expected,
                        "<=": actual <= expected,
                        "==": actual == expected,
                    }[predicate.operator]
                    else "UNSATISFIED"
                )
        elif predicate.fact == "repair_history" and predicate.operator == "==":
            if predicate.value not in {"YES", "NO"}:
                raise ValueError()
            if value in {"YES", "NO"}:
                result = "SATISFIED" if value == predicate.value else "UNSATISFIED"
        elif predicate.fact == "allowed_delivery_methods" and predicate.operator == "contains":
            if predicate.value not in {"shipping", "pickup"}:
                raise ValueError()
            if value:
                result = "SATISFIED" if predicate.value in value else "UNSATISFIED"
        else:
            result = "UNSUPPORTED"
    except (InvalidOperation, ValueError, TypeError):
        result = "UNSUPPORTED"
    return {
        "predicate_id": str(index),
        "fact": predicate.fact,
        "fact_version": fact.version if fact else None,
        "expected_value": predicate.value,
        "operator": predicate.operator,
        "result": result,
    }


def assess(db, product, candidate, now):
    """Read-only, bounded dependencies. Missing independent questions do not block offers."""
    facts = facts_for(db, product.id)
    evaluations = [evaluate(p, facts, i) for i, p in enumerate(candidate.conditions)]
    missing = []
    for field in ("amount", "delivery", "payer"):
        if getattr(candidate, field) is None and not (
            field == "payer" and candidate.delivery == "pickup"
        ):
            missing.append(field)
    for item in evaluations:
        if item["result"] != "SATISFIED":
            missing.append(f"condition:{item['fact']}:{item['result']}")
    allowed = facts.get("allowed_delivery_methods")
    if candidate.delivery:
        if not allowed:
            missing.append("fact:allowed_delivery_methods")
        elif candidate.delivery not in allowed.value["value"]:
            missing.append("delivery_not_allowed")
    if candidate.commitments:
        missing.append("unsupported_commitment")
    cost, discount, rule = Decimal(0), Decimal(0), None
    if candidate.delivery == "pickup" and candidate.payer == "seller":
        missing.append("inconsistent_delivery")
    if candidate.delivery == "shipping" and candidate.payer == "seller":
        if not candidate.region:
            missing.append("region")
        else:
            rules = list(
                db.scalars(
                    select(ShippingCostRule).where(
                        ShippingCostRule.product_id == product.id,
                        ShippingCostRule.region == candidate.region,
                        ShippingCostRule.delivery == "shipping",
                        ShippingCostRule.active.is_(True),
                        ShippingCostRule.expires_at > now,
                    )
                )
            )
            if len(rules) != 1:
                missing.append("cost_unknown" if not rules else "cost_ambiguous")
            else:
                rule = rules[0]
                cost, discount = rule.shipping_cost, rule.discount
    snapshot = {
        "rule_id": rule.id if rule else None,
        "rule_version": rule.version if rule else None,
        "shipping_cost": str(cost),
        "discount": str(discount),
        "delivery_fact_version": allowed.version if allowed else None,
    }
    return (
        missing,
        evaluations,
        snapshot,
        min(now + timedelta(hours=24), rule.expires_at) if rule else now + timedelta(hours=24),
    )


def quote_in_transaction(db, product, candidate, now):
    missing, evaluations, snapshot, expiry = assess(db, product, candidate, now)
    if missing:
        raise Conflict("报价依据尚未满足：" + ", ".join(missing))
    quote = CostQuote(
        id=uuid4().hex,
        product_id=product.id,
        scope={
            "region": candidate.region,
            "delivery": candidate.delivery,
            "payer": candidate.payer or "buyer",
        },
        snapshot=snapshot,
        created_at=now,
        expires_at=expiry,
    )
    db.add(quote)
    db.flush()
    terms = Terms(
        amount=candidate.amount,
        shipping_paid_by=candidate.payer or "buyer",
        shipping_cost=Decimal(snapshot["shipping_cost"]),
        seller_discount=Decimal(snapshot["discount"]),
        additional_terms={"delivery_method": candidate.delivery},
    )
    return quote, terms, evaluations


def valid_basis(db, product, offer, now):
    basis = db.get(SemanticOfferBasis, offer.id)
    if not basis:
        return False
    quote = db.get(CostQuote, basis.cost_quote_id)
    if not quote or quote.product_id != product.id or quote.expires_at <= now:
        return False
    candidate = Candidate.model_validate(basis.candidate)
    missing, evaluations, snapshot, _ = assess(db, product, candidate, now)
    return not missing and snapshot == quote.snapshot and evaluations == basis.evaluations


def public_answer(db, product, topic, source_quote=""):
    aliases = {
        "battery_health": "battery_health_pct",
        "repair": "repair_history",
        "delivery": "allowed_delivery_methods",
        "price": "listed_price",
        "listing_price": "listed_price",
        "confirmation": "confirmation_button",
    }
    topic = aliases.get(topic, topic)
    if topic == "allowed_delivery_methods" and source_quote and not re.search(
        r"自提|面交|快递|配送|寄|邮|送货|运费|交易方式", source_quote
    ):
        topic = "unknown"
    # A free-form model topic must not hide a concrete question from the fallback renderer.
    topics = []
    for name, pattern in {
        "listed_price": r"标价|页面价格|挂牌价|公开价格",
        "accessories": r"配件|充电线|充电器|包装盒",
        "invoice": r"发票|购买凭证",
        "confirmation_button": r"确认按钮|确认入口|点.{0,4}确认|确认.{0,5}(?:含义|意思)",
    }.items():
        if re.search(pattern, source_quote):
            topics.append(name)
    if topics:
        if topic in {*FACTS, "description", "product", "title"} and topic not in topics:
            topics.insert(0, topic)
        answers = [public_answer(db, product, name) for name in topics]
        return (
            "UNKNOWN" if any(a[0] == "UNKNOWN" for a in answers) else "ANSWERED",
            " ".join(a[1] for a in answers),
            None,
        )
    if topic == "listed_price":
        return "ANSWERED", f"页面公开标价为{product.listed_price}元；标价不表示已成交。", None
    if topic == "confirmation_button":
        return (
            "ANSWERED",
            (
                "确认按钮用于确认你已查看的、当前有效报价及其条款，记录双方交易意向；"
                "不会自动付款或发货。请先核对金额、配送方式和条件；未同意时不要点击。"
            ),
            None,
        )
    labels = {
        "accessories": ("配件及包装", r"配件|包装|充电线|充电器"),
        "invoice": ("发票或购买凭证", r"发票|凭证"),
        "warranty": ("原厂保修期限", r"保修"),
        "screen": ("屏幕划痕或坏点", r"屏幕"),
        "condition": ("成色和使用痕迹", r"成新|新|痕迹"),
    }
    if topic in labels:
        label, pattern = labels[topic]
        if re.search(pattern, product.description or ""):
            return (
                "ANSWERED",
                (
                    f"关于{label}，卖家描述原文为：{product.description}"
                    " 未在描述中写明的具体项目仍未知，不能据此保证。"
                ),
                None,
            )
        return "UNKNOWN", f"卖家尚未提供可核实的{label}资料，当前未知。", None
    fact = facts_for(db, product.id).get(topic)
    if fact and fact.value["value"] not in (None, "UNKNOWN"):
        value = fact.value["value"]
        if topic == "battery_health_pct":
            answer = f"电池健康 {value}%"
        elif topic == "repair_history":
            answer = "有维修记录" if value == "YES" else "无维修记录"
        else:
            answer = "支持" + "、".join("自提" if v == "pickup" else "快递" for v in value)
        return "ANSWERED", f"卖家提供的资料：{answer}。", fact.version
    if topic in {"description", "product", "title"}:
        return "ANSWERED", f"卖家商品描述：{product.title}；{product.description}", None
    label = source_quote[:120] if source_quote else "这项"
    return "UNKNOWN", f"关于“{label}”，卖家尚未提供可核实的资料，当前未知。", None


def clarification(candidate, missing):
    """Explain the actual pending fields and predicates without changing their scope."""
    labels = {
        "amount": "报价金额",
        "delivery": "自提或快递",
        "payer": "运费由谁承担",
        "region": "收货区域（无需完整地址）",
        "cost_unknown": "卖家核实该区域运费",
        "cost_ambiguous": "卖家消除冲突的运费规则",
        "delivery_not_allowed": "当前不支持所选配送方式",
        "fact:allowed_delivery_methods": "卖家核实支持的配送方式",
        "dependency_unresolved": "先解决报价所依赖的问题",
    }
    parts = [labels[m] for m in missing if m in labels]
    statuses = {
        "UNKNOWN": "资料未知，需核实",
        "UNSATISFIED": "现有资料不满足",
        "UNSUPPORTED": "当前无法核实该条件",
    }
    for predicate in candidate.conditions:
        for status, text in statuses.items():
            if f"condition:{predicate.fact}:{status}" in missing:
                parts.append(f"“{predicate.source_quote}”：{text}")
    if "unsupported_commitment" in missing:
        parts.append("当前不支持承诺：" + "、".join(candidate.commitments))
    return "报价尚未提交，已保留原条件。待解决：" + "；".join(parts or ["核实原报价前提"]) + "。"
