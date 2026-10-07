import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import ContextVar
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from uuid import uuid4

from app.agent.decision_provider import (
    LangChainDecisionProvider,
    budget_contract,
    native_tool_choice,
    response_format_contract,
)
from app.agent.message_protocol import encode_message
from app.agent.model_factory import QwenChatModelFactory
from app.agent.runtime_state import (
    AgentBudgetExceeded,
    call_budget,
    call_model_parameters,
    freeze_config,
)
from app.agent.token_budget import TokenBudgetManager
from app.core.config import get_settings
from app.services.pricing_service import OfferTerms, ShippingPayer, calculate_net_income
from evaluation.dynamic_buyer import (
    BuyerAction,
    DynamicBuyerProfile,
    ModelDynamicBuyer,
    ScriptedDynamicBuyer,
    validate_action,
)
from evaluation.dynamic_metrics import summarize_dynamic
from evaluation.dynamic_models import ScriptedAgentProvider
from evaluation.dynamic_safety import AUDIT_VERSION, observe
from evaluation.environment import EvaluationEnvironment
from evaluation.production_driver import ProductionDriver
from evaluation.scenarios import load_scenarios


class DynamicCallTracker:
    def __init__(
        self,
        provider,
        config,
        *,
        max_calls=2000,
        max_tokens=5000000,
        max_cost=Decimal("20"),
        timeout=3600,
        buyer_provider=None,
    ):
        self.provider, self.config = provider, config
        self.buyer_provider = buyer_provider or provider
        self.max_calls, self.max_tokens, self.max_cost = max_calls, max_tokens, max_cost
        self.deadline = time.monotonic() + timeout
        self.reserved_tokens = 0
        self.reserved_cost = Decimal("0")
        self.calls = []
        self.run_id = ContextVar("dynamic_run_id", default="")
        self.lock = RLock()
        self.journal_path = None

    def journal(self, record):
        if self.journal_path:
            with self.lock, self.journal_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def __call__(self, messages, tools, *, purpose, manifest):
        budget = call_budget(self.config, purpose)
        count = TokenBudgetManager(budget).check(messages, budget_contract(tools, purpose))
        reserve_tokens = count + budget["output_tokens"]
        prices = self.config["prices"]
        cost = (
            Decimal(count) * Decimal(prices["input"] or "0")
            + Decimal(budget["output_tokens"]) * Decimal(prices["output"] or "0")
        ) / Decimal("1000000")
        record = {
            "call_id": uuid4().hex,
            "run_id": self.run_id.get(),
            "purpose": purpose,
            "status": "STARTED",
            "request": {
                "messages": [encode_message(m) for m in messages],
                "tools": tools,
                "tool_choice": native_tool_choice(purpose) if tools else None,
                "parallel_tool_calls": False if tools else None,
                "model": call_model_parameters(self.config, purpose),
                "response_format": response_format_contract(purpose),
                "max_output_tokens": budget["output_tokens"],
            },
            "manifest": manifest,
            "usage": None,
            "estimated_cost": None,
            "reserved_tokens": reserve_tokens,
            "reserved_cost": str(cost),
            "estimated_input_tokens": count,
            "initial_reserved_tokens": reserve_tokens,
            "initial_reserved_cost": str(cost),
        }
        with self.lock:
            if (
                len(self.calls) >= self.max_calls
                or self.reserved_tokens + reserve_tokens > self.max_tokens
                or self.reserved_cost + cost > self.max_cost
                or time.monotonic() >= self.deadline
            ):
                raise AgentBudgetExceeded("BATCH_BUDGET")
            self.reserved_tokens += reserve_tokens
            self.reserved_cost += cost
            self.calls.append(record)
            self.journal(record)
        started = time.perf_counter()
        try:
            provider = self.buyer_provider if purpose == "BUYER_SIMULATION" else self.provider
            response = provider.invoke_agent(messages, tools, self.config, purpose=purpose)
            record["response"] = encode_message(response)
            record["status"] = "SUCCEEDED"
            record["usage"] = response.usage_metadata or None
            if record["usage"]:
                usage = record["usage"]
                cached = usage.get("input_token_details", {}).get("cache_read", 0)
                actual = (
                    Decimal(usage["input_tokens"] - cached) * Decimal(prices["input"] or "0")
                    + Decimal(cached) * Decimal(prices["cached"] or prices["input"] or "0")
                    + Decimal(usage["output_tokens"]) * Decimal(prices["output"] or "0")
                ) / Decimal("1000000")
                record["estimated_cost"] = str(actual) if prices["currency"] else None
                with self.lock:
                    self.reserved_cost += max(Decimal("0"), actual - cost)
                    self.reserved_tokens += max(0, usage.get("total_tokens", 0) - reserve_tokens)
                    record["reserved_cost"] = str(max(actual, cost))
                    record["reserved_tokens"] = max(usage.get("total_tokens", 0), reserve_tokens)
            return response
        except Exception as exc:
            record["status"] = "UNKNOWN"
            record["error_category"] = type(exc).__name__
            raise
        finally:
            record["duration_ms"] = round((time.perf_counter() - started) * 1000)
            self.journal(record)


def public_view(driver, turn):
    env = driver.environment
    offer = env.offers.get(env.current_offer_id)
    approved = env.approval and env.approval["status"] == "APPROVE"
    return {
        "turn": turn,
        "messages": [
            {
                "role": "buyer" if r.kind == "BUYER_INPUT" else "seller",
                "content": r.payload["content"],
            }
            for r in driver.stream
            if r.visibility == "PUBLIC"
        ],
        "current_offer": offer,
        "confirmable": bool(
            offer and (offer["proposer"] == "AGENT" or offer["status"] == "ACCEPTED" or approved)
        ),
        "negotiation_status": env.status,
        "round_count": env.round_count,
        "max_rounds": env.scenario.policy.max_rounds,
        "can_submit_buyer_offer": (
            env.status == "ACTIVE" and env.round_count < env.scenario.policy.max_rounds
        ),
    }


def run_episode(
    scenario, group, config, tracker, *, seed, trial, buyer_mode="scripted", profiles=None
):
    env = EvaluationEnvironment(scenario)
    driver = ProductionDriver(env, config, group, tracker)
    run_id = f"{scenario.scenario_id}:{group}:{trial}:{seed}"
    tracker.run_id.set(run_id)
    profiles = profiles or load_profiles()
    profile = DynamicBuyerProfile.model_validate(
        {
            **profiles["defaults"],
            "style": profiles["category_styles"].get(scenario.category.value, "reasonable"),
            **profiles.get("scenario_overrides", {}).get(scenario.scenario_id, {}),
        }
    ).model_dump()
    buyer = ModelDynamicBuyer(tracker) if buyer_mode == "qwen" else ScriptedDynamicBuyer(seed)
    first = scenario.turns[0]
    adaptation = None
    if first.offer and first.offer.price > scenario.buyer_goal.maximum_price:
        # Preserve the original fixture; the derived dynamic case records this adaptation.
        action = BuyerAction(action="ASK", message="我想了解价格和交易条件。")
        adaptation = "INITIAL_OFFER_ABOVE_BUYER_BUDGET_CHANGED_TO_INQUIRY"
    else:
        action = BuyerAction(
            action="OFFER" if first.offer else "ASK", message=first.message, offer=first.offer
        )
    status, error, error_detail = "UNRESOLVED", None, None
    seller_latency = 0
    buyer_questions = []
    started = time.perf_counter()
    for turn in range(profile["max_turns"]):
        # Buyer action/domain validation is separate from seller execution.
        # A simulator exceeding the offer limit must not count as a seller failure.
        try:
            validate_action(action, env, scenario.buyer_goal)
            if action.action == "CLOSE":
                env.status = "CLOSED"
                status = "CLOSED_VALID"
                break
            if action.action == "CONFIRM":
                env.confirm(action.offer_id, scenario.buyer_goal)
                observe(env, phase="BUYER_CONFIRM")
                status = "AGREED"
                break
        except Exception as exc:
            status, error = "BUYER_SIMULATOR_FAILURE", type(exc).__name__
            error_detail = {"phase": "BUYER_ACTION", "message": str(exc)[:1000]}
            break
        try:
            if action.action == "ASK":
                buyer_questions.append(action.message)
            seller_started = time.perf_counter()
            result = driver.run_turn(action)
            seller_latency += round((time.perf_counter() - seller_started) * 1000)
            observe(env, phase="SELLER_REPLY", reply=result.reply, seq=driver.stream[-1].seq)
            runtime_stop = driver.last_backend and driver.last_backend.checkpoint.get(
                "stop_reason"
            ) not in {None, "COMPLETED"}
            if result.outcome.value == "MODEL_ERROR" or (
                result.outcome.value == "SAFE_FAILURE" and runtime_stop
            ):
                status = (
                    "BATCH_BUDGET_EXHAUSTED"
                    if driver.last_backend.checkpoint.get("stop_reason") == "BATCH_BUDGET"
                    else "SELLER_FAILURE"
                )
                break
            notification = env.process_approval()
            if notification:
                driver.append("APPROVAL_NOTIFICATION", {"content": notification}, "PUBLIC")
                observe(
                    env,
                    phase="APPROVAL_NOTIFICATION",
                    reply=notification,
                    seq=driver.stream[-1].seq,
                )
        except AgentBudgetExceeded as exc:
            status, error = "BATCH_BUDGET_EXHAUSTED", str(exc)
            break
        except Exception as exc:
            status, error = "SELLER_FAILURE", type(exc).__name__
            error_detail = {"phase": "SELLER_EXECUTION", "message": str(exc)[:1000]}
            break
        view = public_view(driver, turn + 1)
        buyer_error = None
        for _attempt in range(2):
            try:
                repaired_view = (
                    {**view, "previous_action_error": buyer_error} if buyer_error else view
                )
                action = buyer.next_action(repaired_view, scenario.buyer_goal, profile)
                if (
                    buyer_mode == "qwen"
                    and view["turn"] < profile["min_consultation_turns"]
                    and action.action != "ASK"
                ):
                    raise ValueError(
                        "Long-history stress buyer must finish the required consultations"
                    )
                validate_action(action, env, scenario.buyer_goal)
                break
            except AgentBudgetExceeded as exc:
                status, error = "BATCH_BUDGET_EXHAUSTED", str(exc)
                break
            except Exception as exc:
                buyer_error = str(exc)[:1000]
        else:
            status, error = "BUYER_SIMULATOR_FAILURE", buyer_error
            error_detail = {"phase": "BUYER_SIMULATION", "message": buyer_error}
            break
        if status == "BATCH_BUDGET_EXHAUSTED":
            break
        # max_turns bounds seller turns. The buyer may confirm or close after
        # the final seller reply without needing another seller/model turn.
        if action.action == "CLOSE":
            env.status, status = "CLOSED", "CLOSED_VALID"
            break
        if action.action == "CONFIRM":
            env.confirm(action.offer_id, scenario.buyer_goal)
            observe(env, phase="BUYER_CONFIRM")
            status = "AGREED"
            break
    else:
        status = "UNRESOLVED"
    observe(env, phase="EPISODE_END")
    net = None
    if env.confirmed_offer_id:
        offer = env.offers[env.confirmed_offer_id]
        net = str(
            calculate_net_income(
                OfferTerms(
                    Decimal(offer["price"]),
                    ShippingPayer(offer["shipping_paid_by"]),
                    Decimal(offer["shipping_cost"]) if offer["shipping_cost"] is not None else None,
                    Decimal(offer["seller_borne_discount"]),
                )
            )
        )
    tool_errors = [
        r
        for r in driver.stream
        if r.kind == "TOOL_RESULT"
        and "TOOL_INPUT_REJECTED" in r.payload["message"]["data"]["content"]
    ]
    return {
        "run_id": run_id,
        "scenario_id": scenario.scenario_id,
        "scenario_version": scenario.scenario_version,
        "category": scenario.category.value,
        "tags": scenario.tags,
        "group": group,
        "seed": seed,
        "trial": trial,
        "status": status,
        "error": error,
        "error_detail": error_detail,
        "adaptation": adaptation,
        "feasible": scenario.buyer_goal.maximum_price >= scenario.policy.minimum_net_price
        and all(
            k == "delivery_method" and v in {"pickup", "shipping"}
            for k, v in scenario.buyer_goal.required_terms.items()
        ),
        "seller_net_income": net,
        "violations": env.violations,
        "safety_audit_version": AUDIT_VERSION,
        "safety_observations": env.safety_observations,
        "seller_turns": driver.turn,
        "public_message_count": sum(r.visibility == "PUBLIC" for r in driver.stream),
        "repeated_questions": len(buyer_questions) - len(set(buyer_questions)),
        "tool_input_rejections": len(tool_errors),
        "memory_update_failures": int(
            any(b.checkpoint.get("memory_update_failed") for b in driver.turn_backends)
        ),
        "memory_update_failure_turns": sum(
            bool(b.checkpoint.get("memory_update_failed")) for b in driver.turn_backends
        ),
        "memory_update_failure_events": sum(
            b.checkpoint.get("memory_update_failure_count", 0) for b in driver.turn_backends
        ),
        "memory_update_failure_details": [
            {"task_id": b.task_id, **detail}
            for b in driver.turn_backends
            for detail in b.checkpoint.get("memory_update_failure_details", [])
        ],
        "seller_latency_ms": seller_latency,
        "episode_duration_ms": round((time.perf_counter() - started) * 1000),
        "profile": profile,
        "final_memory": driver.context["memory"],
        "commitments": env.commitments,
        "stream": [asdict(r) for r in driver.stream],
    }


def run_batch(
    scenarios,
    config,
    tracker,
    *,
    groups=("G0", "G1", "G2", "G3", "G4"),
    repeats=3,
    seed=20261006,
    buyer_mode="scripted",
    progress=None,
    workers=1,
    profiles=None,
):
    runs = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                run_episode,
                scenario,
                group,
                config,
                tracker,
                seed=seed + trial,
                trial=trial,
                buyer_mode=buyer_mode,
                profiles=profiles,
            )
            for scenario in scenarios
            for trial in range(repeats)
            for group in groups
        ]
        for future in as_completed(futures):
            result = future.result()
            runs.append(result)
            if progress:
                progress(len(runs), result)
    return runs


def load_profiles():
    return json.loads(
        (Path(__file__).parent / "scenarios" / "dynamic_profiles_v1.json").read_text(
            encoding="utf-8"
        )
    )


def select_balanced(scenarios, count):
    buckets = {}
    for scenario in scenarios:
        buckets.setdefault(scenario.category.value, []).append(scenario)
    selected = []
    while len(selected) < count and any(buckets.values()):
        for bucket in buckets.values():
            if bucket and len(selected) < count:
                selected.append(bucket.pop(0))
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description="V4 dynamic buyer G0-G4 isolated evaluation")
    parser.add_argument("--scenario-file", type=Path)
    parser.add_argument("--scenario-id", action="append")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--profiles-file", type=Path)
    parser.add_argument("--protocol-file", type=Path)
    parser.add_argument("--seed", type=int, default=20261006)
    parser.add_argument("--failure-rate-limit", type=float, default=0.1)
    parser.add_argument(
        "--groups",
        nargs="+",
        choices=["G0", "G1", "G2", "G3", "G4"],
        default=["G0", "G1", "G2", "G3", "G4"],
    )
    parser.add_argument("--model", choices=["scripted", "qwen"], default="scripted")
    parser.add_argument("--buyer-model", choices=["scripted", "qwen"], default="scripted")
    parser.add_argument("--allow-real-model", action="store_true")
    parser.add_argument("--max-calls", type=int, default=2000)
    parser.add_argument("--max-tokens", type=int, default=5000000)
    parser.add_argument("--max-cost", type=Decimal, default=Decimal("20"))
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.count < 1 or args.repeats < 1 or len(args.groups) != len(set(args.groups)):
        parser.error("Positive sample counts and unique groups are required")
    if not 0 <= args.failure_rate_limit <= 1:
        parser.error("Failure rate limit must be between 0 and 1")
    if not 1 <= args.workers <= 8:
        parser.error("workers must be between 1 and 8")
    if args.output and (args.output / "manifest.json").exists():
        parser.error("Refusing to overwrite a recorded evaluation batch; choose a fresh output")
    # Each tracker record must correspond to one RPC; hidden SDK retries break accounting.
    settings = get_settings().model_copy(update={"model_max_retries": 0})
    real = args.model == "qwen" or args.buyer_model == "qwen"
    if real and (not args.allow_real_model or not settings.model_cost_is_configured):
        parser.error("Real calls require --allow-real-model and configured model prices")
    config = freeze_config(settings)
    real_provider = (
        LangChainDecisionProvider(QwenChatModelFactory().create(settings))
        if real
        else ScriptedAgentProvider()
    )
    provider = real_provider if args.model == "qwen" else ScriptedAgentProvider()
    tracker = DynamicCallTracker(
        provider,
        config,
        max_calls=args.max_calls,
        max_tokens=args.max_tokens,
        max_cost=args.max_cost,
        timeout=args.timeout,
        buyer_provider=real_provider if args.buyer_model == "qwen" else ScriptedAgentProvider(),
    )
    dataset = load_scenarios(args.scenario_file)
    eligible = [
        s for s in dataset.scenarios if not args.scenario_id or s.scenario_id in args.scenario_id
    ]
    selected = select_balanced(eligible, args.count)
    if not selected:
        parser.error("No scenarios selected")
    output = args.output or Path("evaluation/results") / f"v4-{uuid4().hex[:12]}"
    output.mkdir(parents=True, exist_ok=False)
    tracker.journal_path = output / "call_events.jsonl"
    profiles = (
        json.loads(args.profiles_file.read_text(encoding="utf-8"))
        if args.profiles_file
        else load_profiles()
    )
    manifest = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "scenario_set_version": dataset.version,
        "scenario_set_hash": dataset.content_hash,
        "source_scenario_ids": [s.scenario_id for s in selected],
        "groups": args.groups,
        "repeats": args.repeats,
        "model_is_mock": not real,
        "seller_mode": args.model,
        "buyer_mode": args.buyer_model,
        "profiles": profiles,
        "profiles_hash": hashlib.sha256(json.dumps(profiles, sort_keys=True).encode()).hexdigest(),
        "workers": args.workers,
        "seed": args.seed,
        "sdk_max_retries": 0,
        "safety_audit_version": AUDIT_VERSION,
        "frozen_protocol": json.loads(args.protocol_file.read_text(encoding="utf-8"))
        if args.protocol_file
        else None,
        "protocol_sha256": hashlib.sha256(args.protocol_file.read_bytes()).hexdigest()
        if args.protocol_file
        else None,
        "implementation_sha256": {
            str(path.relative_to(Path(__file__).parent.parent)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for directory in (
                Path(__file__).parent,
                Path(__file__).parent.parent / "app" / "agent",
                Path(__file__).parent.parent / "app" / "services",
                Path(__file__).parent.parent / "app" / "db" / "models",
            )
            for path in directory.rglob("*.py")
        },
        "config": config,
        "budget": {
            "max_calls": args.max_calls,
            "max_tokens": args.max_tokens,
            "max_cost": str(args.max_cost),
            "timeout": args.timeout,
        },
        "limitations": [
            "Dynamic paths vary; seed alone cannot fix remote model sampling",
            "Agreement means intent, not payment or a completed sale",
            "Naturalness and persuasion require calibrated blind review",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def progress(count, result):
        if count % 10 == 0:
            print(f"Completed {count}: {result['group']} {result['status']}", flush=True)
        with (output / "runs.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")

    runs = run_batch(
        selected,
        config,
        tracker,
        groups=tuple(args.groups),
        repeats=args.repeats,
        seed=args.seed,
        buyer_mode=args.buyer_model,
        progress=progress,
        workers=args.workers,
        profiles=profiles,
    )
    with (output / "calls.jsonl").open("w", encoding="utf-8") as f:
        for call in tracker.calls:
            f.write(json.dumps(call, ensure_ascii=False) + "\n")
    summary = summarize_dynamic(runs, tracker.calls)
    all_recorded = len(runs) == len(selected) * args.repeats * len(args.groups)
    gate = {
        "passed": all_recorded
        and tracker.reserved_cost <= args.max_cost
        and all(not r["violations"] and r["status"] != "BATCH_BUDGET_EXHAUSTED" for r in runs)
        and all(
            g["seller_failure_rate"] <= args.failure_rate_limit
            and g["buyer_failure_rate"] <= args.failure_rate_limit
            for g in summary.values()
        ),
        "checks": {
            "budget_reserved_cost": str(tracker.reserved_cost),
            "budget_reserved_tokens": tracker.reserved_tokens,
            "runs": len(runs),
            "all_requested_runs_recorded": all_recorded,
            "seller_failure_rate_limit": args.failure_rate_limit,
            "buyer_failure_rate_limit": args.failure_rate_limit,
            "failure_rates_by_group": {
                group: {key: g[key] for key in ("seller_failure_rate", "buyer_failure_rate")}
                for group, g in summary.items()
            },
        },
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "gate.json").write_text(
        json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest.update(
        completed_at=datetime.now(UTC).isoformat(),
        reserved_cost=str(tracker.reserved_cost),
        artifacts_sha256={
            name: hashlib.sha256((output / name).read_bytes()).hexdigest()
            for name in ["runs.jsonl", "calls.jsonl", "summary.json"]
        },
    )
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"output": str(output), "gate": gate}, ensure_ascii=False))
    return 0 if gate["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
