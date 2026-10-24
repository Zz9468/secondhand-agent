"""Explicitly bounded, synthetic-only Qwen capability probe. No automatic SDK retries."""

import argparse
import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path

from dotenv import dotenv_values
from openai import OpenAI

from app.p0.model import ProbeBudget, Proposal, parse_proposal

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-model", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("refusing to overwrite an existing probe")
    config = dotenv_values(ROOT / ".env")
    budget = ProbeBudget()
    client = OpenAI(
        api_key=config["MODEL_API_KEY"],
        base_url=config["MODEL_BASE_URL"],
        timeout=30,
        max_retries=0,
    )
    report = {
        "model": config["MODEL_NAME"],
        "thinking": False,
        "attempts": [],
        "prompt_version": "p0-capability-v3",
        "max_calls": 6,
        "max_reserved_cny": "0.10",
        "max_tokens": 20000,
        "pricing_reference": "https://help.aliyun.com/zh/model-studio/qwen3-7-flash",
        "price_cny_per_million": {"input": "0.2", "output": "0.8"},
    }

    def call(kind, **kwargs):
        request = {
            "model": config["MODEL_NAME"],
            "temperature": 0,
            "max_tokens": 512,
            "extra_body": {"enable_thinking": False},
            **kwargs,
        }
        budget.reserve(json.dumps(request, ensure_ascii=False))
        attempt = {
            "kind": kind,
            "status": "reserved",
            "request_hash": hashlib.sha256(
                json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }
        report["attempts"].append(attempt)
        save()
        start = time.monotonic()
        try:
            response = client.chat.completions.create(**request)
            attempt.update(
                status="returned",
                seconds=round(time.monotonic() - start, 4),
                usage=response.usage.model_dump() if response.usage else None,
            )
            if not response.usage or response.usage.total_tokens <= 0:
                raise ValueError("missing token accounting")
            return response
        except Exception as error:
            attempt.update(
                status="failed",
                error_type=type(error).__name__,
                http_status=getattr(error, "status_code", None),
                seconds=round(time.monotonic() - start, 4),
            )
            # Deliberately never serialize provider exception text/request headers.
            raise
        finally:
            save()

    def save():
        report["calls_reserved"] = budget.calls
        report["reserved_cny"] = str(budget.reserved)
        report["reserved_tokens"] = budget.tokens
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "proposal",
                "strict": True,
                "schema": Proposal.model_json_schema(),
            },
        }
        cases = [
            ("800元自提可以吗", "PROPOSE_OFFER", Decimal("800"), "pickup"),
            ("别人卖800，我预算也差不多，先看看", "NONE", None, None),
            ("这部手机有没有维修过", "ASK_PRODUCT", None, None),
        ]
        for text, expected, expected_amount, expected_delivery in cases:
            kwargs = dict(
                messages=[
                    {
                        "role": "system",
                        "content": "仅做语义提取，返回JSON。"
                        "PROPOSE_OFFER=买家提出当前交易价格；800自提可以吗属于明确报价。"
                        "ASK_PRODUCT=询问商品事实；NONE=仅表达预算、比价等非报价信息。"
                        "source_quote逐字摘取输入；amount提取明确报价金额并转为纯数字字符串，"
                        "例如800元写成字符串800，报价里有金额时绝不能填null；"
                        "仅当不是报价或确实没有金额时amount用null。"
                        "配送方式：自提=pickup，邮寄=shipping，未说明则null。",
                    },
                    {"role": "user", "content": text},
                ],
                response_format=response_format,
            )
            if response_format["type"] == "json_object":
                kwargs["messages"][0]["content"] += json.dumps(Proposal.model_json_schema())
            try:
                response = call("structured", **kwargs)
            except Exception as error:
                if (
                    getattr(error, "status_code", None) != 400
                    or response_format["type"] != "json_schema"
                ):
                    raise
                response_format = {"type": "json_object"}
                kwargs["response_format"] = response_format
                kwargs["messages"][0]["content"] += json.dumps(Proposal.model_json_schema())
                response = call("json_fallback", **kwargs)
            parsed = parse_proposal(response.choices[0].message.content, text)
            report["attempts"][-1]["validated"] = parsed.model_dump()
            assert parsed.intent == expected
            assert (
                Decimal(parsed.amount) if parsed.amount is not None else None
            ) == expected_amount
            assert parsed.delivery == expected_delivery
        report["structured_mode"] = response_format["type"]
        tool = {
            "type": "function",
            "function": {
                "name": "get_product_info",
                "description": "查询公开商品资料",
                "parameters": {
                    "type": "object",
                    "properties": {"product_id": {"type": "string"}},
                    "required": ["product_id"],
                    "additionalProperties": False,
                },
            },
        }
        response = call(
            "tool",
            messages=[{"role": "user", "content": "查询商品P0-phone"}],
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": "get_product_info"}},
        )
        tools = response.choices[0].message.tool_calls
        assert len(tools) == 1 and tools[0].function.name == "get_product_info"
        assert json.loads(tools[0].function.arguments) == {"product_id": "P0-phone"}
        report["tool_arguments_valid"] = True
        report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["error_type"] = type(error).__name__
    finally:
        known_cost = Decimal(0)
        for attempt in report["attempts"]:
            usage = attempt.get("usage")
            if usage:
                known_cost += (
                    Decimal(usage["prompt_tokens"]) * Decimal("0.2")
                    + Decimal(usage["completion_tokens"]) * Decimal("0.8")
                ) / 1_000_000
        report["known_estimated_cny"] = str(known_cost)
        save()
        client.close()
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("passed", "calls_reserved", "reserved_cny", "known_estimated_cny")
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
