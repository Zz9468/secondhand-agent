"""Typed seller evidence and deterministic evaluation; never infer costs from chat."""

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


def public_answer(db, product, topic):
    aliases = {
        "battery_health": "battery_health_pct",
        "repair": "repair_history",
        "delivery": "allowed_delivery_methods",
    }
    topic = aliases.get(topic, topic)
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
    return "UNKNOWN", "卖家尚未提供可核实的这项资料。", None
