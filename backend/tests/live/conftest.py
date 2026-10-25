"""Opt-in real-model evidence, persisted even when a graph assertion fails."""

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import ApprovalRequest, Message, NegotiationSession
from app.db.models.graph import (
    BusinessActionReceipt,
    GraphTurnQuality,
    ModelCallLedger,
    NegotiationInput,
)
from app.db.models.semantic import SemanticTurn
from app.semantic.model import PROMPT_VERSION, SemanticModel, SemanticResponseError
from tests.graph.conftest import graph_factory  # noqa: F401
from tests.graph.test_semantic import semantic_case  # noqa: F401


class LiveEvidence:
    def __init__(self, path):
        self.path = path
        self.report = {
            "started_at": datetime.now(UTC).isoformat(),
            "source_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "prompt_version": PROMPT_VERSION,
            "attempts": [],
            "cases": [],
        }
        self.report["working_tree_dirty"] = bool(
            subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
        )

    def save(self):
        attempts = self.report["attempts"]
        self.report["calls"] = len(attempts)
        self.report["actual_tokens"] = sum(a.get("tokens") or 0 for a in attempts)
        self.report["estimated_cny"] = str(
            sum(
                (Decimal(a["estimated_cost"]) for a in attempts if a.get("estimated_cost")),
                Decimal(0),
            )
        )
        self.report["unknown_usage_calls"] = sum(a.get("tokens") is None for a in attempts)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8"
        )


@pytest.fixture(scope="session")
def live_evidence():
    if os.getenv("RUN_P2_REAL_MODEL") != "1" or not os.getenv("P2_LIVE_REPORT"):
        pytest.skip("use scripts.run_p2_live_tests --allow-real-model --output ...")
    path = Path(os.environ["P2_LIVE_REPORT"])
    if path.exists():
        pytest.fail("refusing to overwrite prior live evidence")
    evidence = LiveEvidence(path)
    evidence.save()
    yield evidence
    evidence.report["finished_at"] = datetime.now(UTC).isoformat()
    evidence.save()


@pytest.fixture
def live_case(live_evidence, semantic_case, request):  # noqa: F811
    c = semantic_case

    class RecordedModel(SemanticModel):
        def __call__(self, text, context, config):
            tokens, cost = self.reserve_for_config(text, context, config)
            attempt = {
                "case": request.node.name,
                "text": text,
                "context": context,
                "model": config["model_name"],
                "status": "RESERVED",
                "reserved_tokens": tokens,
                "reserved_cny": str(cost),
            }
            live_evidence.report["attempts"].append(attempt)
            live_evidence.save()
            started = time.monotonic()
            try:
                value, used, actual_cost = super().__call__(text, context, config)
                attempt.update(
                    value=value, tokens=used, estimated_cost=str(actual_cost), status="OK"
                )
                return value, used, actual_cost
            except Exception as exc:
                attempt.update(status="FAILED", error_type=type(exc).__name__)
                if isinstance(exc, SemanticResponseError):
                    attempt.update(
                        raw_content=exc.raw_content,
                        tokens=exc.tokens,
                        estimated_cost=str(exc.cost) if exc.cost is not None else None,
                        validation_error=exc.reason,
                    )
                raise
            finally:
                attempt["seconds"] = round(time.monotonic() - started, 3)
                live_evidence.save()

    c.runner.semantic_provider = RecordedModel()
    yield c
    with c.factory() as db:
        session = db.get(NegotiationSession, c.sid)
        inputs = list(
            db.scalars(
                select(NegotiationInput)
                .where(NegotiationInput.session_id == c.sid)
                .order_by(NegotiationInput.input_seq)
            )
        )
        records = []
        for item in inputs:
            quality = db.get(GraphTurnQuality, item.id)
            turn = db.get(SemanticTurn, item.id)
            receipt = db.get(BusinessActionReceipt, f"turn:{item.id}:action")
            ledgers = list(
                db.scalars(select(ModelCallLedger).where(ModelCallLedger.input_id == item.id))
            )
            reply = db.scalar(select(Message).where(Message.source_id == f"turn:{item.id}:reply"))
            records.append(
                {
                    "run_id": item.id,
                    "text": item.payload["text"],
                    "sequence": item.input_seq,
                    "graph_version": item.graph_version,
                    "prompt_version": item.config["prompt_version"],
                    "quality_version": item.config.get("quality_version"),
                    "active_ms": item.active_ms,
                    "quality": {
                        "approved": quality.approved,
                        "review_count": quality.review_count,
                        "stop_reason": quality.stop_reason,
                        "context_bytes": len(
                            json.dumps(quality.context, ensure_ascii=False).encode()
                        ),
                    }
                    if quality
                    else None,
                    "semantic_results": turn.results if turn else None,
                    "command": turn.command["kind"] if turn else None,
                    "outcome": receipt.result["outcome"] if receipt else None,
                    "reply": reply.content if reply else None,
                    "card": reply.card if reply else None,
                    "ledger": [
                        {
                            "status": row.status,
                            "purpose": row.purpose,
                            "tokens": row.actual_tokens,
                            "estimated_cny": str(row.actual_cost),
                        }
                        for row in ledgers
                    ],
                }
            )
        live_evidence.report["cases"].append(
            {
                "name": request.node.name,
                "session_status": session.status.value,
                "rounds": session.round_count,
                "runs": records,
                "approvals": [
                    {
                        "id": a.id,
                        "offer_id": a.offer_id,
                        "status": a.status.value,
                        "effect_status": a.effect_status,
                        "effect_revision": a.effect_revision,
                        "notification_status": a.notification_status,
                        "invalid_reason": a.invalid_reason,
                    }
                    for a in db.scalars(
                        select(ApprovalRequest).where(ApprovalRequest.session_id == c.sid)
                    )
                ],
                "test_outcome": getattr(request.node, "live_outcome", "unknown"),
            }
        )
    live_evidence.save()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        item.live_outcome = report.outcome
