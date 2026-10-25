"""Independent approval threads; durable events are hints, database decisions are authority."""

from contextlib import suppress

from langgraph.types import Command
from sqlalchemy import select

from app.db.models.graph import ApprovalExecution, BusinessActionReceipt, NotificationDelivery
from app.graphs.approval.graph import build_approval_graph
from app.persistence.checkpointer import locked_saver
from app.runtime.contracts import StaleOwner, utcnow
from app.runtime.recovery import RecoveryConflict
from app.services.approval_lifecycle import approval_thread
from app.services.graph_approval_service import GraphApprovalService
from app.services.notification_service import NotificationService


class ApprovalRunner:
    def __init__(self, factory, postgres_url, *, clock=utcnow, fault=lambda point: None):
        self.factory, self.postgres_url, self.clock, self.fault = (
            factory,
            postgres_url,
            clock,
            fault,
        )
        self.commands = GraphApprovalService(factory, clock=clock)

    def publish(self, source):
        service = NotificationService(self.factory, clock=self.clock)
        with self.factory() as db:
            item = db.get(NotificationDelivery, source)
            if item.status == "PUBLISHED":
                return item.public_message_id
        token = service.claim(source)
        if not token:
            return None  # Durable independent notification obligation remains pending.
        try:
            return service.publish(None, source=source, notification_token=token)
        except Exception:
            service.retry(source, token)
            raise

    def run(self, approval_id):
        tid = approval_thread(approval_id)
        config = {"configurable": {"thread_id": tid}}
        with locked_saver(self.postgres_url, tid, instance=f"approval-{approval_id}") as (saver, _):
            owner = self.commands.claim(approval_id)
            if not owner:
                return None
            try:
                self.fault("after_claim")
                self.commands.reconcile(approval_id)
                graph = build_approval_graph(saver, self.commands, owner, self.publish, self.fault)
                state = graph.get_state(config)
                with self.factory() as db:
                    execution = db.get(ApprovalExecution, approval_id)
                    has_applied = db.scalar(
                        select(BusinessActionReceipt.id)
                        .where(
                            BusinessActionReceipt.id.like(f"approval:{approval_id}:apply:%"),
                        )
                        .limit(1)
                    )
                    if execution.graph_version != "approval-v1" or (
                        (execution.checkpoint_ref or has_applied) and not state.values
                    ):
                        raise RecoveryConflict("审批 checkpoint 缺失或版本不匹配")
                if state.values and (
                    state.values.get("approval_id") != approval_id
                    or state.values.get("graph_version") != "approval-v1"
                ):
                    raise RecoveryConflict("审批 checkpoint 归属或版本不匹配")
                pending = self.commands.pending_events(approval_id)
                requested = f"approval:{approval_id}:requested"
                if state.values and not state.next and state.values.get("result"):
                    # END may have persisted before event acknowledgement. Do not replay effects.
                    self.commands.acknowledge(
                        approval_id,
                        self.commands.covered_events(approval_id, state.values["result"]),
                        owner,
                    )
                    pending = self.commands.pending_events(approval_id)
                if not state.values or (not state.next and pending):
                    graph.invoke(
                        {
                            "approval_id": approval_id,
                            "graph_version": "approval-v1",
                            "event_id": None,
                            "result": None,
                            "public_message_id": None,
                        },
                        config=config,
                        durability="sync",
                    )
                elif state.next:
                    interrupted = any(task.interrupts for task in state.tasks)
                    event_id = self.commands.terminal_event(approval_id)
                    if not interrupted or event_id:
                        graph.invoke(
                            Command(resume={"event_id": event_id}) if interrupted else None,
                            config=config,
                            durability="sync",
                        )
                final = graph.get_state(config)
                self.fault("after_checkpoint")
                if not final.next:
                    self.fault("after_end")
                    self.commands.acknowledge(
                        approval_id,
                        self.commands.covered_events(approval_id, final.values["result"]),
                        owner,
                    )
                else:
                    # A terminal event racing load/interrupt is intentionally left PENDING.
                    self.commands.acknowledge(approval_id, [requested], owner)
                self.commands.release(
                    approval_id, owner, checkpoint=final.config["configurable"]["checkpoint_id"]
                )
                return final.values
            except Exception as exc:
                with suppress(StaleOwner):
                    self.commands.release(
                        approval_id, owner, failed=True, recovery=isinstance(exc, RecoveryConflict)
                    )
                raise
