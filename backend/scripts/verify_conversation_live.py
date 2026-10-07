"""Published dialogue through frontend proxy, HTTP API, MySQL and actual model."""

import argparse
import json
import re
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from sqlalchemy import select

from app.agent.claim_safety import find_unsafe_claims
from app.agent.conversation_reply import ConversationFinish, preliminary_error
from app.db.models import AgentCallSnapshot, Message, ModelExecutionTask, Offer
from app.db.session import get_session_factory
from scripts.evaluate_live_workflow import ApiClient
from scripts.verify_v4_live import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:5173")
    args = parser.parse_args()
    ledger = json.loads(Path("evaluation/results/v4-budget-ledger.json").read_text())
    if Decimal(ledger["conservative_allocated_cost"]) + Decimal("0.20") > Decimal("20"):
        raise ValueError("Cumulative budget leaves insufficient allowance for four live tasks")
    if args.output.exists():
        raise ValueError("Use a fresh live verification directory")
    prepare(args.output, args.api_url)
    setup = json.loads((args.output / "setup.json").read_text())
    sid = setup["session_id"]
    credentials = json.loads((args.output / "buyer-credentials.json").read_text())
    buyer = ApiClient(args.api_url, timeout_seconds=150)
    buyer.request("POST", "/api/auth/login", credentials)
    cases = [
        ("greeting", "你好", [r"你好|您好|嗨"]),
        ("product", "我想知道商品信息", [r"89", r"配件|配齐"]),
        ("negotiability", "是否支持讲价？", [r"支持讲价|可以.{0,6}(?:讨论|讲价|商量|协商|议价)"]),
        ("warranty", "还剩多久原厂保修？", [r"保修", r"没有|没|未|不|核实|确认"]),
    ]
    rows = []
    try:
        for case_id, content, requirements in cases:
            request_id = "conversation-live-" + case_id
            payload = {"request_id": request_id, "content": content}
            started = perf_counter()
            _, result = buyer.request(
                "POST", f"/api/negotiations/{sid}/messages", payload, expected_status=201
            )
            elapsed = perf_counter() - started
            reply = result["agent_message"]["content"]
            with get_session_factory()() as db:
                task = db.scalar(
                    select(ModelExecutionTask)
                    .where(ModelExecutionTask.session_id == sid)
                    .order_by(ModelExecutionTask.id.desc())
                )
                assert task is not None
                snapshots = list(
                    db.scalars(
                        select(AgentCallSnapshot).where(AgentCallSnapshot.model_task_id == task.id)
                    )
                )
                assert task.status.value == "SUCCEEDED"
                assert task.input_snapshot["agent_config"]["model"]["thinking"] is True
                assert task.input_snapshot["agent_config"]["reply_strategy"] == "conversation-v1"
                assert any(c.purpose == "REPLY_REVIEW" for c in snapshots)
                assert db.scalar(select(Offer).where(Offer.session_id == sid)) is None
            passed = (
                result["outcome"] in {"INFORMATIONAL", "CLARIFICATION"}
                and all(re.search(p, reply) for p in requirements)
                and not find_unsafe_claims(reply)
                and not preliminary_error(
                    ConversationFinish.model_validate(
                        {
                            "decision": {
                                "action": "INQUIRY",
                                "reason": "核对最终公开回复",
                                "reply": reply,
                            }
                        }
                    )
                )
            )
            rows.append(
                {
                    "case": case_id,
                    "task_id": task.id,
                    "reply": reply,
                    "outcome": result["outcome"],
                    "duration_seconds": round(elapsed, 2),
                    "passed": passed,
                }
            )
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
            if case_id == "negotiability":
                _, replay = buyer.request(
                    "POST", f"/api/negotiations/{sid}/messages", payload, expected_status=201
                )
                assert (
                    replay["idempotent_replay"]
                    and replay["agent_message"]["id"] == result["agent_message"]["id"]
                )
        with get_session_factory()() as db:
            public = list(db.scalars(select(Message).where(Message.session_id == sid)))
            assert len(public) == 22 + 2 * len(cases)
    finally:
        setup["task_ids"] = [r["task_id"] for r in rows]
        (args.output / "setup.json").write_text(json.dumps(setup, indent=2), encoding="utf-8")
        report = {
            "status": "PASS"
            if len(rows) == len(cases) and all(r["passed"] for r in rows)
            else "FAIL",
            "cases": rows,
            "synthetic_data": True,
            "session_id": sid,
        }
        (args.output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    assert report["status"] == "PASS", "Published HTTP dialogue failed"


if __name__ == "__main__":
    main()
