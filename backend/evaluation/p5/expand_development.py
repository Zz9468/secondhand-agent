"""Frozen AI-authored regressions for known omissions and uncovered facts (not holdouts)."""

import json
from copy import deepcopy

from app.runtime.contracts import digest
from evaluation.p5.datasets import DATA, load_cases


def main():
    base = next(c for c in load_cases() if c["case_id"] == "risk-offer-3")
    specifications = [
        ("listed-price", "页面标价到底多少？只核对价格，不报价。", ["1000"]),
        ("accessories-unknown", "包装盒、充电器和配件有哪些？", ["配件", "未知"]),
        ("invoice-unknown", "有没有原始发票或购买凭证？", ["发票", "未知"]),
        ("button-meaning", "确认按钮是什么意思？我没同意购买。", ["交易意向", "不会自动付款"]),
        ("price-and-invoice", "标价多少，原始发票还在吗？", ["1000", "发票", "未知"]),
        ("battery-and-price", "电池健康多少，页面标价多少？", ["92", "1000"]),
        ("specific-condition", "只有电池至少98%才买，900自提。", ["98", "不满足"]),
        ("negative-alternatives", "电池不到98%我就不买，900自提或者950包邮到上海。", []),
        ("invoice-condition", "原始发票必须在，才能执行900自提。", ["发票"]),
        ("repair-condition", "从没维修过才算数，900自提。", ["维修"]),
        ("shipping-region", "900包邮可以吗？", ["区域"]),
        ("shipping-rule", "900包邮到上海可以吗？", ["运费"]),
        ("warranty-condition", "900自提，但必须保修一年。", ["保修"]),
        ("pending-button", "800自提可以吗？", []),
        ("quote-not-mine", "聊天记录里他说900自提，不是我报价。", []),
        ("invoice-not-confirm", "发票可以看看吗？不是接受报价。", ["发票"]),
    ]
    cases = []
    for name, text, required in specifications:
        case = deepcopy(base)
        case.update(
            case_id="dev-coverage-" + name,
            version="p5-dev-coverage-v1",
            family="development-" + name,
            split="development",
            used_for_tuning=True,
            turns=[text],
            required_reply=required,
            expected_outcomes=["NO_ACTION"],
            risk_class=None,
            annotation_kind="AI_development_regression",
            label_basis="Known P5 omissions plus explicit coverage gaps; not human labels",
        )
        if name == "pending-button":
            case.update(
                turns=[text, "等待卖家时，确认按钮是什么意思？先不确认。"],
                expected_outcomes=["WAITING_APPROVAL", "NO_ACTION"],
                expected_status="WAITING_APPROVAL",
                required_reply=["交易意向"],
            )
        cases.append(case)
    known = deepcopy(cases[1])
    known.update(
        case_id="dev-coverage-accessories-known",
        family="development-accessory-source",
        product=dict(
            title="二手平板", listed_price="1680.50", description="含原装包装和充电线；不含充电器。"
        ),
        required_reply=["原装包装和充电线", "不含充电器"],
    )
    cases.append(known)
    with (DATA / "coverage_development.json").open("x", encoding="utf-8") as stream:
        json.dump(
            dict(version="p5-dev-coverage-v1", cases=cases, content_sha256=digest(cases)),
            stream,
            ensure_ascii=False,
            indent=2,
        )
    print(len(cases))


if __name__ == "__main__":
    main()
