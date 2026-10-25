"""薄执行器：先锁线程、再领取队首，核对官方 checkpoint 后启动/恢复/收尾。"""

from contextlib import suppress

from sqlalchemy import func, select

from app.db.models import ModelExecutionTask, ModelTaskStatus, NegotiationSession
from app.db.models.graph import (
    BusinessActionReceipt,
    GraphTurnQuality,
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
        self,
        factory,
        postgres_url,
        *,
        queue=None,
        fault=lambda point: None,
        semantic_provider=None,
        quality_provider=None,
    ):
        self.queue = queue or InputQueue(factory)
        self.factory, self.postgres_url, self.fault = factory, postgres_url, fault
        self.semantic_provider = semantic_provider
        self.quality_provider = quality_provider

    def run(self, run_id):
        from app.observability.graph_tracing import graph_trace

        with graph_trace():
            return self._run(run_id)

    def _run(self, run_id):
        run = self.queue.read(run_id)
        config = {
            "configurable": {"thread_id": thread_id(run["session_id"])},
            "metadata": {"run_ref": digest(run_id), "graph_version": run["graph_version"]},
        }
        with locked_saver(
            self.postgres_url, config["configurable"]["thread_id"], instance=run_id
        ) as (saver, _):
            with self.factory() as db:
                task = db.get(ModelExecutionTask, run["task_id"])
                if db.get(NegotiationSession, run["session_id"]).archived_at:
                    raise Conflict("会话已归档，历史请求不能重新执行")
                if task.status == ModelTaskStatus.SUCCEEDED:
                    return task.result_snapshot
            owner = self.queue.claim(run_id)
            if not owner:
                return None
            try:
                self.fault("after_claim")
                from app.semantic.contracts import QUALITY_VERSION, SEMANTIC_VERSIONS

                if run["graph_version"] not in {GRAPH_VERSION, *SEMANTIC_VERSIONS}:
                    raise RecoveryConflict("当前 Worker 不支持该图版本")
                if (
                    digest(run["payload"]) != run["input_hash"]
                    or digest(run["config"]) != run["config_hash"]
                ):
                    raise RecoveryConflict("不可变输入或配置已损坏")
                if run["graph_version"] in SEMANTIC_VERSIONS:
                    from app.graphs.negotiation.semantic_graph import build_semantic_graph

                    graph = build_semantic_graph(
                        saver,
                        self.queue,
                        owner,
                        fault=self.fault,
                        provider=self.semantic_provider,
                        quality=run["graph_version"] == QUALITY_VERSION,
                        quality_provider=self.quality_provider,
                    )
                else:
                    graph = build_graph(saver, self.queue, owner, fault=self.fault)
                snapshot = graph.get_state(config)
                with self.factory() as db:
                    terminate = db.get(ModelExecutionTask, run["task_id"]).last_manual_action
                if terminate == "TERMINATE":
                    from app.services.business_command_service import BusinessCommandService

                    if snapshot.next and snapshot.values.get("run_id") != run_id:
                        raise RecoveryConflict("不能终止其他输入的 checkpoint")
                    # Explicit seller termination writes a terminal checkpoint without replaying
                    # a semantic/action node. Existing committed receipts remain authoritative.
                    BusinessCommandService(self.factory, clock=self.queue.clock).abort_or_query(
                        run_id, owner, reason="卖家已停止本次处理，请根据当前结果继续协商。"
                    )
                    message_id = NotificationService(self.factory, clock=self.queue.clock).publish(
                        run_id, owner=owner
                    )
                    graph.update_state(
                        config,
                        {**new_turn(run), "published_message_id": message_id},
                        as_node="publish_reply",
                    )
                    final = graph.get_state(config)
                    self.queue.finalize(run_id, owner, final.values, final.next)
                    return {"public_message_id": message_id}
                with self.factory() as db:
                    current = db.get(NegotiationInput, run_id)
                    session = db.get(NegotiationSession, run["session_id"])
                    evidence = bool(current.checkpoint_ref)
                    for model, field in (
                        (GraphTurnQuality, "input_id"),
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
                    from app.runtime.heartbeat import keep_lease

                    with keep_lease(self.queue, run_id, owner):
                        graph.invoke(
                            {
                                **new_turn(run),
                                **(
                                    {
                                        "semantic_reference": None,
                                        "context_reference": None,
                                        "repair_count": 0,
                                        "candidate_valid": False,
                                        "reply_approved": False,
                                    }
                                    if run["graph_version"] in SEMANTIC_VERSIONS
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
                    return notifications.publish(run_id, source=source, notification_token=token)
                except Exception:
                    notifications.retry(source, token)
                    raise
        return None
