"""薄执行器：先锁线程、再领取队首，核对官方 checkpoint 后启动/恢复/收尾。"""

from contextlib import suppress

from sqlalchemy import func, select

from app.db.models import ModelExecutionTask, ModelTaskStatus, NegotiationSession
from app.db.models.graph import (
    BusinessActionReceipt,
    ModelCallLedger,
    NegotiationInput,
    NotificationDelivery,
)
from app.graphs.negotiation.graph import build_graph, new_turn
from app.persistence.checkpointer import locked_saver
from app.runtime.contracts import GRAPH_VERSION, Conflict, StaleOwner, digest, thread_id
from app.runtime.input_queue import InputQueue
from app.runtime.recovery import Entry, RecoveryConflict, RunIdentity, choose_entry
from app.services.notification_service import NotificationService


class GraphRunner:
    def __init__(
        self, factory, postgres_url, *, queue=None, fault=lambda point: None, semantic_provider=None
    ):
        self.queue = queue or InputQueue(factory)
        self.factory, self.postgres_url, self.fault = factory, postgres_url, fault
        self.semantic_provider = semantic_provider

    def run(self, run_id):
        run = self.queue.read(run_id)
        config = {"configurable": {"thread_id": thread_id(run["session_id"])}}
        with locked_saver(
            self.postgres_url, config["configurable"]["thread_id"], instance=run_id
        ) as (saver, _):
            with self.factory() as db:
                task = db.get(ModelExecutionTask, run["task_id"])
                if task.status == ModelTaskStatus.SUCCEEDED:
                    return task.result_snapshot
            owner = self.queue.claim(run_id)
            if not owner:
                return None
            try:
                self.fault("after_claim")
                from app.semantic.contracts import VERSION

                if run["graph_version"] not in {GRAPH_VERSION, VERSION}:
                    raise RecoveryConflict("当前 Worker 不支持该图版本")
                if (
                    digest(run["payload"]) != run["input_hash"]
                    or digest(run["config"]) != run["config_hash"]
                ):
                    raise RecoveryConflict("不可变输入或配置已损坏")
                if run["graph_version"] == VERSION:
                    from app.graphs.negotiation.semantic_graph import build_semantic_graph

                    graph = build_semantic_graph(
                        saver, self.queue, owner, fault=self.fault, provider=self.semantic_provider
                    )
                else:
                    graph = build_graph(saver, self.queue, owner, fault=self.fault)
                snapshot = graph.get_state(config)
                with self.factory() as db:
                    current = db.get(NegotiationInput, run_id)
                    session = db.get(NegotiationSession, run["session_id"])
                    evidence = bool(current.checkpoint_ref)
                    for model, field in (
                        (ModelCallLedger, "input_id"),
                        (BusinessActionReceipt, "input_id"),
                        (NotificationDelivery, "input_id"),
                    ):
                        evidence |= bool(
                            db.scalar(
                                select(func.count())
                                .select_from(model)
                                .where(
                                    getattr(model, field) == run_id,
                                )
                            )
                        )
                    entry = choose_entry(
                        RunIdentity(
                            run_id,
                            run["input_seq"],
                            run["input_hash"],
                            run["config_hash"],
                            run["graph_version"],
                        ),
                        snapshot.values,
                        snapshot.next,
                        has_evidence=evidence,
                        is_head=session.last_resolved_input_seq + 1 == run["input_seq"],
                    )
                if entry == Entry.HISTORY:
                    raise RecoveryConflict("历史任务不能恢复到后续轮次")
                if entry != Entry.FINALIZE:
                    graph.invoke(
                        {
                            **new_turn(run),
                            **(
                                {"semantic_reference": None}
                                if run["graph_version"] == VERSION
                                else {}
                            ),
                        }
                        if entry == Entry.START
                        else None,
                        config=config,
                        durability="sync",
                    )
                final = graph.get_state(config)
                self.fault("after_end")
                self.queue.finalize(run_id, owner, final.values, final.next)
                return {"public_message_id": final.values["published_message_id"]}
            except Exception as exc:
                with suppress(StaleOwner):
                    self.queue.release(
                        run_id, owner, recovery_required=isinstance(exc, RecoveryConflict)
                    )
                raise

    def notify(self, source):
        with self.factory() as db:
            item = db.get(NotificationDelivery, source)
            if not item:
                raise Conflict("通知不存在")
            source_thread, run_id = item.source_thread_id, item.input_id
        # 不读取 checkpoint，也不调用旧图或推进输入游标。
        with locked_saver(self.postgres_url, source_thread, instance=f"notify-{run_id}"):
            notifications = NotificationService(self.factory, clock=self.queue.clock)
            token = notifications.claim(source)
            if token:
                try:
                    return notifications.publish(run_id, notification_token=token)
                except Exception:
                    notifications.retry(source, token)
                    raise
        return None
