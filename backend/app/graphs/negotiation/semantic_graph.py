"""P2 graph: references in checkpoints, full semantic evidence in the local audit store."""

from langgraph.graph import END, START, StateGraph

from app.db.models import NegotiationSession
from app.graphs.negotiation.graph import TurnState
from app.runtime.contracts import BudgetExhausted, Conflict, thread_id
from app.runtime.input_queue import lock_owner
from app.semantic.contracts import ChatInput, IntentFrame
from app.semantic.model import SemanticModel, parsing_contexts
from app.semantic.service import load_command, parser_context, plan_turn
from app.services.business_command_service import BUSINESS_ERRORS, BusinessCommandService
from app.services.model_call_ledger_service import ModelCallLedgerService
from app.services.notification_service import NotificationService


class SemanticState(TurnState):
    semantic_reference: dict | None


def build_semantic_graph(saver, queue, owner, *, provider=None, fault=lambda point: None):
    model = provider or SemanticModel()
    ledger = ModelCallLedgerService(queue.factory, clock=queue.clock)
    commands = BusinessCommandService(queue.factory, clock=queue.clock)
    notifications = NotificationService(queue.factory, clock=queue.clock)

    def parse(state):
        checkpoint = saver.get_tuple(
            {"configurable": {"thread_id": thread_id(state["session_id"])}}
        )
        with queue.factory() as db, db.begin():
            run, _ = lock_owner(db, state["run_id"], owner, queue.clock())
            if not checkpoint or checkpoint.checkpoint["channel_values"].get("run_id") != run.id:
                raise Conflict("初始 checkpoint 尚未持久化")
            run.checkpoint_ref = checkpoint.checkpoint["id"]
            session = db.get(NegotiationSession, run.session_id)
            payload = ChatInput.model_validate(run.payload)
            context = parser_context(db, run, session, queue.clock())
            config = run.config
        if payload.action:
            frame = {
                "intents": [
                    {
                        "id": "action",
                        "type": "CONFIRM_INTENT"
                        if payload.action == "CONFIRM"
                        else "CLOSE_SESSION",
                        "source_quote": payload.text,
                    }
                ]
            }
            plan_turn(queue.factory, state["run_id"], owner, frame, queue.clock())
            return {"semantic_reference": None}
        reference = None
        for attempt_context in parsing_contexts(context, config["prompt_version"]):
            tokens, cost = model.reserve(payload.text, attempt_context)

            def invoke(call_context=attempt_context):
                value, used, actual_cost = model(payload.text, call_context, config)
                frame = IntentFrame.model_validate(value)
                frame.validated_for(payload.text)
                return frame.model_dump(mode="json"), used, actual_cost

            try:
                reference = ledger.call(
                    state["run_id"],
                    owner,
                    "parse-intents-v1",
                    invoke,
                    reserved_tokens=tokens,
                    reserved_cost=cost,
                )
                break
            except ValueError:
                continue
            except BudgetExhausted:
                break
        if reference is None:
            # Invalid semantics become a no-action clarification, never a guessed proposal.
            frame = {
                "ambiguous": True,
                "intents": [{"id": "clarify", "type": "OTHER", "source_quote": payload.text}],
            }
            plan_turn(queue.factory, state["run_id"], owner, frame, queue.clock())
            return {"semantic_reference": None}
        fault("after_semantics")
        return {"semantic_reference": reference}

    def plan(state):
        if state.get("semantic_reference"):
            frame = ledger.resolve(state["run_id"], "parse-intents-v1", state["semantic_reference"])
            plan_turn(queue.factory, state["run_id"], owner, frame, queue.clock())
        fault("after_plan")
        with queue.factory() as db, db.begin():
            run, _ = lock_owner(db, state["run_id"], owner, queue.clock())
            session = db.get(NegotiationSession, run.session_id)
            return {"expected_version": session.version}

    def commit(state):
        with queue.factory() as db:
            from app.db.models.graph import NegotiationInput

            payload = load_command(db, db.get(NegotiationInput, state["run_id"]))
        try:
            result = (
                commands.abort_or_query(state["run_id"], owner)
                if payload.kind == "QUERY"
                else commands.execute(state["run_id"], owner, state["expected_version"])
            )
        except BUSINESS_ERRORS as exc:
            result = commands.abort_or_query(state["run_id"], owner, reason=str(exc))
        fault("after_command")
        return {"business_result": {"outcome": result["outcome"]}}

    def publish(state):
        message_id = notifications.publish(state["run_id"], owner=owner)
        fault("after_publish")
        return {"published_message_id": message_id}

    graph = StateGraph(SemanticState)
    for name, node in (
        ("parse_intents", parse),
        ("plan_intents", plan),
        ("commit_business", commit),
        ("publish_reply", publish),
    ):
        graph.add_node(name, node)
    for source, target in (
        (START, "parse_intents"),
        ("parse_intents", "plan_intents"),
        ("plan_intents", "commit_business"),
        ("commit_business", "publish_reply"),
        ("publish_reply", END),
    ):
        graph.add_edge(source, target)
    return graph.compile(checkpointer=saver)
