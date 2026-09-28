"""按非敏感关联键导出本地观测事件或聚合摘要。"""

import argparse
import json
from collections import Counter, defaultdict
from decimal import Decimal

from app.db.session import get_session_factory
from app.observability.service import ObservabilityEventSnapshot, ObservabilityService


def summarize(events: tuple[ObservabilityEventSnapshot, ...]) -> dict[str, object]:
    model_calls = [item for item in events if item.event_type == "MODEL_CALL_COMPLETED"]
    measured = [item for item in model_calls if item.total_tokens is not None]
    costs: defaultdict[str, Decimal] = defaultdict(Decimal)
    for item in model_calls:
        if item.estimated_cost is not None and item.cost_currency is not None:
            costs[item.cost_currency] += item.estimated_cost
    return {
        "event_count": len(events),
        "event_types": dict(Counter(item.event_type for item in events)),
        "model_call_count": len(model_calls),
        "model_call_error_count": sum(item.outcome == "ERROR" for item in model_calls),
        "token_coverage_count": len(measured),
        "input_tokens": sum(item.input_tokens or 0 for item in measured),
        "output_tokens": sum(item.output_tokens or 0 for item in measured),
        "cached_input_tokens": sum(
            item.cached_input_tokens or 0 for item in measured
        ),
        "total_tokens": sum(item.total_tokens or 0 for item in measured),
        "estimated_cost_by_currency": {
            currency: str(value) for currency, value in sorted(costs.items())
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="导出脱敏的本地观测记录")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--session-id", type=int)
    selector.add_argument("--correlation-id")
    selector.add_argument("--model-task-id", type=int)
    parser.add_argument("--error-category")
    parser.add_argument("--min-attempt-count", type=int)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--format", choices=("jsonl", "summary"), default="jsonl")
    args = parser.parse_args()

    events = ObservabilityService(get_session_factory()).list_events(
        session_id=args.session_id,
        correlation_id=args.correlation_id,
        model_task_id=args.model_task_id,
        error_category=args.error_category,
        min_attempt_count=args.min_attempt_count,
        limit=args.limit,
    )
    if args.format == "summary":
        print(json.dumps(summarize(events), ensure_ascii=False, sort_keys=True))
        return
    for event in events:
        print(json.dumps(event.to_json_dict(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
