from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import Offer, Product
from tests.graph.test_p4_quality import quality_case  # noqa: F401
from tests.graph.test_semantic import (
    candidate,
    fact,
    intent,
    message,
    semantic_case,  # noqa: F401
    turn,
)


@pytest.mark.parametrize(
    "text,required",
    [
        ("标价多少？", ["1234.56"]),
        ("配件和发票有没有？", ["原装包装和充电线", "发票", "未知"]),
        ("确认按钮是什么意思？先别确认。", ["交易意向", "不会自动付款"]),
    ],
)
def test_specific_public_answers_survive_quality_fallback(quality_case, text, required):  # noqa: F811
    c = quality_case
    c.quality.error = True
    with c.factory() as db, db.begin():
        product = db.get(Product, c.pid)
        product.listed_price = Decimal("1234.56")
        product.description = "配原装包装和充电线。"
    run = turn(c, text, [intent(text, kind="ASK_PRODUCT", topic="unknown")])
    answer = message(c, run).content
    assert all(value in answer for value in required)
    with c.factory() as db:
        assert not db.scalar(select(Offer).where(Offer.session_id == c.sid))


def test_unmet_condition_identifies_original_requirement(quality_case):  # noqa: F811
    c = quality_case
    fact(c, "battery_health_pct", 92)
    text = "电池至少98%才买，900自提。"
    run = turn(
        c,
        text,
        [
            intent(
                text,
                candidate=candidate(
                    conditions=[
                        {
                            "fact": "battery_health_pct",
                            "operator": ">=",
                            "value": "98",
                            "source_quote": "电池至少98%",
                        }
                    ]
                ),
            )
        ],
    )
    answer = message(c, run).content
    assert "电池至少98%" in answer and "不满足" in answer and "尚未提交" in answer
    with c.factory() as db:
        assert not db.scalar(select(Offer).where(Offer.session_id == c.sid))
