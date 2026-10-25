"""Explicit bounded P2 model calibration. Synthetic inputs only; never writes business state."""

import argparse
import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.semantic.contracts import IntentFrame
from app.semantic.model import (
    PROMPT_VERSION,
    SemanticModel,
    SemanticResponseError,
    parsing_contexts,
)
from app.semantic.service import safe_frame

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "backend/evaluation/datasets/p2_semantic_labels.json"


def validate_case(case, value):
    frame = IntentFrame.model_validate(value)
    frame.validated_for(case["text"])
    kinds = {item.type for item in frame.intents}
    if case.get("no_action"):
        assert kinds <= {"OTHER", "PROVIDE_INFO", "ASK_PRODUCT", "ASK_TERMS"}
    if case.get("types"):
        assert (
            bool(kinds & set(case["types"]))
            if case.get("any_type")
            else set(case["types"]) <= kinds
        )
    if case.get("ambiguous"):
        assert frame.ambiguous
    elif not case.get("no_action"):
        assert safe_frame(frame, case["text"])[1]
    assert len(frame.intents) >= case.get("min_intents", 1)
    candidates = [i.candidate.model_dump(mode="json") for i in frame.intents if i.candidate]
    for expected in case.get("candidates", []):
        matches = []
        for candidate in candidates:
            matches.append(
                all(
                    (
                        {tuple(p[k] for k in ("fact", "operator", "value")) for p in candidate[key]}
                        == {tuple(p[k] for k in ("fact", "operator", "value")) for p in value}
                    )
                    if key == "conditions"
                    else (
                        Decimal(candidate[key]) == Decimal(value)
                        if key == "amount" and value is not None and candidate[key] is not None
                        else candidate[key] == value
                    )
                    for key, value in expected.items()
                )
            )
        assert any(matches), "candidate mismatch"
    if case.get("alternatives"):
        offers = [i for i in frame.intents if i.type == "PROPOSE_OFFER"]
        assert len(offers) == 2 and offers[0].alternative_group
        assert offers[0].alternative_group == offers[1].alternative_group
    if case.get("commitment"):
        assert any(c["commitments"] for c in candidates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-model", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--max-calls", type=int, default=30, help="0 disables this batch limit")
    parser.add_argument(
        "--max-cny", type=Decimal, default=Decimal("1"), help="0 disables batch limit"
    )
    args = parser.parse_args()
    if args.max_calls < 0 or args.max_cny < 0:
        parser.error("limits must be non-negative")
    if args.output.exists():
        raise RuntimeError("refusing to overwrite prior evidence")
    model = SemanticModel()
    # This upper reservation is deliberately conservative; unknown calls keep it.
    cap, reserved, tokens = args.max_cny, Decimal(0), 0
    cases = json.loads(args.dataset.read_text(encoding="utf-8"))
    report = {
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "prompt_version": PROMPT_VERSION,
        "model": model.settings.model_name,
        "started_at": datetime.now(UTC).isoformat(),
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "max_calls": args.max_calls,
        "max_reserved_cny": str(cap),
        "attempts": [],
    }

    def save():
        report.update(reserved_cny=str(reserved), reserved_tokens=tokens)
        report["actual_tokens"] = sum(a.get("tokens") or 0 for a in report["attempts"])
        report["estimated_cny"] = str(
            sum(
                (
                    Decimal(a["estimated_cost"])
                    for a in report["attempts"]
                    if a.get("estimated_cost")
                ),
                Decimal(0),
            )
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    report["case_results"] = {}
    for case in cases:
        context = case.get(
            "context",
            {"has_bound_draft": False, "missing_fields": [], "has_displayed_offer": False},
        )
        for attempt_index, call_context in enumerate(parsing_contexts(context, PROMPT_VERSION)):
            token_bound, cost_bound = model.reserve(case["text"], call_context)
            if cost_bound is None:
                raise RuntimeError("CNY pricing is unknown for this model/endpoint; no call made")
            if (args.max_calls and len(report["attempts"]) >= args.max_calls) or (
                cap and reserved + cost_bound > cap
            ):
                report["stopped"] = "BUDGET_LIMIT"
                break
            reserved += cost_bound
            tokens += token_bound
            attempt = {
                "id": case["id"],
                "attempt_index": attempt_index,
                "status": "RESERVED",
                "reserved_cny": str(cost_bound),
                "reserved_tokens": token_bound,
            }
            report["attempts"].append(attempt)
            save()
            started = time.monotonic()
            retryable = False
            try:
                frame, actual_tokens, actual_cost = model(
                    case["text"],
                    call_context,
                    {"model_name": model.settings.model_name, "prompt_version": PROMPT_VERSION},
                )
                attempt.update(
                    value=frame,
                    tokens=actual_tokens,
                    estimated_cost=str(actual_cost) if actual_cost is not None else None,
                )
                validate_case(case, frame)
                attempt["status"] = "PASSED"
            except Exception as exc:
                attempt.update(status="FAILED", error_type=type(exc).__name__)
                if isinstance(exc, SemanticResponseError):
                    retryable = True
                    attempt.update(
                        raw_content=exc.raw_content,
                        tokens=exc.tokens,
                        estimated_cost=str(exc.cost) if exc.cost is not None else None,
                        validation_error=exc.reason,
                    )
            attempt["seconds"] = round(time.monotonic() - started, 3)
            report["case_results"][case["id"]] = attempt["status"]
            save()
            if not retryable:
                break
        if report.get("stopped"):
            break
    report["passed"] = len(report["case_results"]) == len(cases) and all(
        status == "PASSED" for status in report["case_results"].values()
    )
    report["finished_at"] = datetime.now(UTC).isoformat()
    save()
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "calls": len(report["attempts"]),
                "reserved_cny": str(reserved),
            }
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
