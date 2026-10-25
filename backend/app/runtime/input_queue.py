"""所有买家输入共享 FIFO；旧任务及失败标签都不能跳过队首。"""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select, text

from app.db.models import (
    Message,
    MessageRole,
    ModelExecutionTask,
    ModelTaskStatus,
    ModelTaskType,
    NegotiationSession,
    Offer,
    SellerPolicy,
)
from app.db.models.graph import DisplayAck, NegotiationInput, PublicInteractionContext
from app.runtime.contracts import (
    CONFIG,
    GRAPH_VERSION,
    Conflict,
    StaleOwner,
    TypedInput,
    digest,
    utcnow,
)


def bounded_transaction(db):
    db.execute(text("SET SESSION innodb_lock_wait_timeout=2"))
    db.execute(text("SET SESSION max_execution_time=5000"))


def owned_session(db, sid, buyer_id, *, lock=False):
    session = db.scalar(
        select(NegotiationSession)
        .where(
            NegotiationSession.id == sid,
            NegotiationSession.buyer_id == buyer_id,
        )
        .with_for_update()
        if lock
        else select(NegotiationSession).where(
            NegotiationSession.id == sid,
            NegotiationSession.buyer_id == buyer_id,
        )
    )
    if session is None:
        raise Conflict("会话不存在或无权访问")
    return session


def lock_owner(db, run_id, owner, now):
    # 输入不可变，先只读取 task_id，再按任务 → 会话的顺序加锁。
    run = db.get(NegotiationInput, run_id)
    if run is None:
        raise Conflict("运行不存在")
    task = db.get(ModelExecutionTask, run.task_id, with_for_update=True)
    run = db.get(NegotiationInput, run_id, with_for_update=True, populate_existing=True)
    if (
        task.runtime_kind != "LANGGRAPH"
        or task.status != ModelTaskStatus.RUNNING
        or (task.lease_token, task.ownership_epoch) != owner
        or task.lease_expires_at <= now
    ):
        raise StaleOwner("执行所有权已失效")
    return run, task


class InputQueue:
    def __init__(self, factory, *, clock=utcnow):
        self.factory = factory
        self.clock = clock

    def enroll(self, sid, buyer_id, *, version=GRAPH_VERSION):
        """仅供内部迁移/验证：空会话固定图版本，不能混用旧 Runtime。"""
        with self.factory() as db, db.begin():
            session = owned_session(db, sid, buyer_id, lock=True)
            if session.graph_version == version:
                return
            if session.graph_version is not None:
                raise Conflict("已有图版本不能被重新绑定")
            for model in (Message, Offer, ModelExecutionTask):
                if db.scalar(
                    select(func.count()).select_from(model).where(model.session_id == sid)
                ):
                    raise Conflict("只允许新空会话启用图")
            from app.semantic.contracts import SEMANTIC_VERSIONS

            if version not in {GRAPH_VERSION, *SEMANTIC_VERSIONS}:
                raise Conflict("不支持的图版本")
            session.graph_version = version

    def enqueue(self, sid, buyer_id, request_id, payload: TypedInput, *, config=None):
        if not request_id or len(request_id) > 64:
            raise Conflict("请求标识无效")
        from app.semantic.contracts import QUALITY_VERSION, SEMANTIC_VERSIONS, ChatInput

        semantic = isinstance(payload, ChatInput)
        payload = (ChatInput if semantic else TypedInput).model_validate(
            payload.model_dump(mode="json")
        )
        body = payload.model_dump(mode="json")
        fingerprint = digest(body)
        with self.factory() as db, db.begin():
            bounded_transaction(db)
            session = owned_session(db, sid, buyer_id, lock=True)
            if session.archived_at:
                raise Conflict("会话已归档，历史请求不能重新执行")
            if session.graph_version not in (SEMANTIC_VERSIONS if semantic else {GRAPH_VERSION}):
                raise Conflict("会话未启用当前图版本")
            old = db.scalar(
                select(NegotiationInput).where(
                    NegotiationInput.session_id == sid,
                    NegotiationInput.request_id == request_id,
                )
            )
            if old:
                if old.input_hash != fingerprint:
                    raise Conflict("同一请求标识的载荷不一致")
                return old.id
            if session.next_input_seq - session.last_resolved_input_seq > 20:
                raise Conflict("未完成输入已达到 20 条")
            now = self.clock()
            if semantic:
                from app.semantic.service import validate_references

                validate_references(db, session, payload, now)
            seen = 0
            if payload.display_receipt_id:
                ack = db.get(DisplayAck, payload.display_receipt_id)
                context = db.get(PublicInteractionContext, ack.context_id) if ack else None
                if (
                    not context
                    or ack.buyer_id != buyer_id
                    or ack.client_view_id != payload.client_view_id
                    or context.session_id != sid
                    or context.buyer_id != buyer_id
                    or ack.displayed_at > now
                    or context.expires_at <= now
                ):
                    raise Conflict("展示凭据归属、视图或时间无效")
                seen = context.public_seq
            if semantic and config is None:
                from app.core.config import get_settings
                from app.semantic.model import PROMPT_VERSION

                settings = get_settings()
                config = {
                    **CONFIG,
                    "schema": "p2-v1",
                    "max_calls": 2,
                    "max_tokens": 50000,
                    "max_cost": "0.20",
                    "model_name": settings.model_name,
                    "prompt_version": PROMPT_VERSION,
                }
                if session.graph_version == QUALITY_VERSION:
                    from app.semantic.model import P4_PROMPT_VERSION

                    config.update(
                        schema="p4-v1",
                        max_calls=10,
                        max_tokens=180000,
                        max_cost=None,
                        active_seconds=120,
                        quality_version="p4-v2",
                        prompt_version=P4_PROMPT_VERSION,
                    )
            frozen_config = dict(CONFIG if config is None else config)
            if (
                set(frozen_config)
                - {
                    "schema",
                    "max_calls",
                    "max_tokens",
                    "max_cost",
                    "model_name",
                    "prompt_version",
                    "active_seconds",
                    "quality_version",
                }
                or frozen_config.get("schema")
                != (
                    "p4-v1"
                    if session.graph_version == QUALITY_VERSION
                    else "p2-v1"
                    if semantic
                    else CONFIG["schema"]
                )
                or not isinstance(frozen_config.get("max_calls"), int)
                or frozen_config["max_calls"] < 0
                or not isinstance(frozen_config.get("max_tokens"), int)
                or frozen_config["max_tokens"] < 0
                or not isinstance(frozen_config.get("active_seconds", 120), (int, float))
                or not 0 < frozen_config.get("active_seconds", 120) <= 120
            ):
                raise Conflict("运行配置无效")
            run_id = uuid4().hex
            message = Message(
                session_id=sid,
                role=MessageRole.BUYER,
                content=payload.text or payload.kind,
                request_id=f"graph-input:{run_id}",
                request_fingerprint=fingerprint,
            )
            policy = db.scalar(
                select(SellerPolicy).where(SellerPolicy.product_id == session.product_id)
            )
            task = ModelExecutionTask(
                task_type=ModelTaskType.CHAT_DECISION,
                business_key=f"graph:{run_id}",
                session_id=sid,
                session_version=session.version,
                policy_version=policy.version,
                input_snapshot={"run_id": run_id},
                input_snapshot_hash=fingerprint,
                runtime_kind="LANGGRAPH",
                correlation_id=str(uuid4()),
            )
            db.add_all([message, task])
            db.flush()
            db.add(
                NegotiationInput(
                    id=run_id,
                    session_id=sid,
                    task_id=task.id,
                    buyer_message_id=message.id,
                    input_seq=session.next_input_seq,
                    request_id=request_id,
                    payload=body,
                    input_hash=fingerprint,
                    config=frozen_config,
                    config_hash=digest(frozen_config),
                    graph_version=session.graph_version,
                    received_at=now,
                    seen_public_message_seq=seen,
                )
            )
            session.next_input_seq += 1
            return run_id

    def read(self, run_id):
        with self.factory() as db:
            run = db.get(NegotiationInput, run_id)
            if run is None:
                raise Conflict("运行不存在")
            return {column.name: getattr(run, column.name) for column in run.__table__.columns}

    def claim(self, run_id, *, lease_seconds=30):
        """调用者必须已持有来源线程锁。"""
        with self.factory() as db, db.begin():
            bounded_transaction(db)
            run = db.get(NegotiationInput, run_id)
            task = db.get(ModelExecutionTask, run.task_id, with_for_update=True)
            session = db.get(NegotiationSession, run.session_id, with_for_update=True)
            now = self.clock()
            if (
                run.input_seq != session.last_resolved_input_seq + 1
                or session.archived_at
                or run.recovery_required
                or task.status
                in {
                    ModelTaskStatus.SUCCEEDED,
                    ModelTaskStatus.FAILED,
                    ModelTaskStatus.STALE,
                    ModelTaskStatus.CANCELLED,
                }
                or (task.lease_expires_at and task.lease_expires_at > now)
                or (task.next_retry_at and task.next_retry_at > now)
            ):
                return None
            from app.runtime.activity import lease_deadline, settle

            settle(run, now)
            deadline = lease_deadline(db, run, now, lease_seconds)
            from app.db.models.graph import NotificationDelivery

            delivery = db.get(NotificationDelivery, f"turn:{run_id}:reply")
            cleanup_only = task.last_manual_action == "TERMINATE" or (
                delivery and delivery.status == "PUBLISHED"
            )
            if cleanup_only:
                deadline = now + timedelta(seconds=15)
            if deadline <= now:
                task.status = ModelTaskStatus.FAILED
                task.lease_token = task.lease_owner = task.lease_expires_at = None
                run.recovery_required = True
                return None
            if task.attempt_count >= task.max_attempts:
                task.status = ModelTaskStatus.FAILED
                task.next_retry_at = None
                task.lease_token = task.lease_owner = task.lease_expires_at = None
                run.recovery_required = True
                return None
            task.ownership_epoch += 1
            task.lease_token = uuid4().hex
            task.lease_owner = f"graph:{run_id}"
            task.lease_expires_at = deadline
            run.active_since, run.active_until = now, deadline
            task.status = ModelTaskStatus.RUNNING
            task.next_retry_at = None
            task.attempt_count += 1
            return task.lease_token, task.ownership_epoch

    def release(self, run_id, owner, *, recovery_required=False):
        with self.factory() as db, db.begin():
            run, task = lock_owner(db, run_id, owner, self.clock())
            from app.runtime.activity import settle

            settle(run, self.clock())
            task.status = ModelTaskStatus.RETRY_WAIT
            task.next_retry_at = self.clock() + timedelta(seconds=1)
            task.lease_token = task.lease_owner = task.lease_expires_at = None
            run.recovery_required = recovery_required

    def heartbeat(self, run_id, owner):
        from app.runtime.activity import lease_deadline

        with self.factory() as db, db.begin():
            run, task = lock_owner(db, run_id, owner, self.clock())
            deadline = lease_deadline(db, run, self.clock())
            if deadline <= self.clock():
                raise StaleOwner("无模型收尾期限已结束")
            task.lease_expires_at = run.active_until = deadline

    def finalize(self, run_id, owner, values, pending):
        from app.db.models.graph import NotificationDelivery

        with self.factory() as db, db.begin():
            run, task = lock_owner(db, run_id, owner, self.clock())
            session = db.get(NegotiationSession, run.session_id, with_for_update=True)
            if (
                pending
                or values.get("run_id") != run_id
                or values.get("input_seq") != run.input_seq
                or values.get("input_hash") != run.input_hash
                or values.get("config_hash") != run.config_hash
                or values.get("graph_version") != run.graph_version
            ):
                raise Conflict("终态不属于当前输入")
            delivery = db.get(NotificationDelivery, f"turn:{run_id}:reply")
            if (
                delivery is None
                or delivery.status != "PUBLISHED"
                or delivery.public_message_id != values.get("published_message_id")
            ):
                raise Conflict("缺少唯一公开结果")
            if session.last_resolved_input_seq + 1 != run.input_seq:
                raise Conflict("不能回拨历史输入")
            session.last_resolved_input_seq = run.input_seq
            from app.runtime.activity import settle

            settle(run, self.clock())
            task.status = ModelTaskStatus.SUCCEEDED
            task.completed_at = self.clock()
            task.result_snapshot = {"public_message_id": delivery.public_message_id}
            task.lease_token = task.lease_owner = task.lease_expires_at = None
