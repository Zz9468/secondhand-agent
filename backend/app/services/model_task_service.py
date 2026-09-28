import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import and_, case, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    ApprovalRequest,
    ApprovalStatus,
    ModelExecutionTask,
    ModelTaskErrorCategory,
    ModelTaskStatus,
    ModelTaskType,
    NegotiationSession,
    Offer,
    SellerPolicy,
)
from app.services.errors import (
    InvalidModelExecutionTaskError,
    ModelExecutionTaskConflictError,
    ModelExecutionTaskLeaseError,
    ModelExecutionTaskNotFoundError,
)

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    provider: str
    model_name: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ModelExecutionTaskSnapshot:
    id: int
    task_type: ModelTaskType
    business_key: str
    session_id: int
    offer_id: int | None
    approval_id: int | None
    snapshot_schema_version: int
    session_version: int
    policy_version: int
    input_snapshot: dict[str, object]
    input_snapshot_hash: str
    status: ModelTaskStatus
    attempt_count: int
    next_retry_at: datetime | None
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    last_error_category: ModelTaskErrorCategory | None
    last_error_message: str | None
    model_provider: str | None
    model_name: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    estimated_cost: Decimal | None
    result_snapshot: dict[str, object] | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ModelTaskCreationResult:
    task: ModelExecutionTaskSnapshot
    idempotent_replay: bool


class ModelTaskService:
    """创建、领取并转换持久化模型任务，不在这里调用外部模型。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_task(
        self,
        *,
        task_type: ModelTaskType,
        business_key: str,
        session_id: int,
        input_snapshot: dict[str, object],
        offer_id: int | None = None,
        approval_id: int | None = None,
        snapshot_schema_version: int = 1,
    ) -> ModelTaskCreationResult:
        """按业务键幂等创建任务，并从数据库事实捕获版本。"""

        stored_key = self._validated_identifier(
            business_key,
            field_name="业务幂等键",
            max_length=128,
        )
        if not isinstance(task_type, ModelTaskType):
            raise InvalidModelExecutionTaskError("模型任务类型无效")
        if snapshot_schema_version <= 0:
            raise InvalidModelExecutionTaskError("输入快照版本必须大于零")
        stored_input, input_hash = self._validated_json_object(input_snapshot)

        try:
            with self._session_factory() as db, db.begin():
                return self._create_validated_task_in_transaction(
                    db=db,
                    task_type=task_type,
                    business_key=stored_key,
                    session_id=session_id,
                    stored_input=stored_input,
                    input_hash=input_hash,
                    offer_id=offer_id,
                    approval_id=approval_id,
                    snapshot_schema_version=snapshot_schema_version,
                )
        except IntegrityError as exc:
            # 唯一约束是并发创建时的最终判定；相同请求返回已有任务。
            with self._session_factory() as db:
                existing = self._by_business_key(
                    db,
                    task_type=task_type,
                    business_key=stored_key,
                )
                if existing is not None:
                    return self._existing_creation_result(
                        existing,
                        session_id=session_id,
                        offer_id=offer_id,
                        approval_id=approval_id,
                        snapshot_schema_version=snapshot_schema_version,
                        input_hash=input_hash,
                    )
            raise ModelExecutionTaskConflictError("模型任务幂等键已被占用") from exc

    def create_task_in_transaction(
        self,
        *,
        db: Session,
        task_type: ModelTaskType,
        business_key: str,
        session_id: int,
        input_snapshot: dict[str, object],
        offer_id: int | None = None,
        approval_id: int | None = None,
        snapshot_schema_version: int = 1,
    ) -> ModelTaskCreationResult:
        """在调用方事务内原子创建业务事实和模型任务。"""

        stored_key = self._validated_identifier(
            business_key,
            field_name="业务幂等键",
            max_length=128,
        )
        if not isinstance(task_type, ModelTaskType):
            raise InvalidModelExecutionTaskError("模型任务类型无效")
        if snapshot_schema_version <= 0:
            raise InvalidModelExecutionTaskError("输入快照版本必须大于零")
        stored_input, input_hash = self._validated_json_object(input_snapshot)
        return self._create_validated_task_in_transaction(
            db=db,
            task_type=task_type,
            business_key=stored_key,
            session_id=session_id,
            stored_input=stored_input,
            input_hash=input_hash,
            offer_id=offer_id,
            approval_id=approval_id,
            snapshot_schema_version=snapshot_schema_version,
        )

    def _create_validated_task_in_transaction(
        self,
        *,
        db: Session,
        task_type: ModelTaskType,
        business_key: str,
        session_id: int,
        stored_input: dict[str, object],
        input_hash: str,
        offer_id: int | None,
        approval_id: int | None,
        snapshot_schema_version: int,
    ) -> ModelTaskCreationResult:
        existing = self._by_business_key(
            db,
            task_type=task_type,
            business_key=business_key,
        )
        if existing is not None:
            return self._existing_creation_result(
                existing,
                session_id=session_id,
                offer_id=offer_id,
                approval_id=approval_id,
                snapshot_schema_version=snapshot_schema_version,
                input_hash=input_hash,
            )

        session_version, policy_version = self._capture_references(
            db,
            task_type=task_type,
            session_id=session_id,
            offer_id=offer_id,
            approval_id=approval_id,
        )
        task = ModelExecutionTask(
            task_type=task_type,
            business_key=business_key,
            session_id=session_id,
            offer_id=offer_id,
            approval_id=approval_id,
            snapshot_schema_version=snapshot_schema_version,
            session_version=session_version,
            policy_version=policy_version,
            input_snapshot=stored_input,
            input_snapshot_hash=input_hash,
            status=ModelTaskStatus.PENDING,
            attempt_count=0,
        )
        db.add(task)
        db.flush()
        db.refresh(task)
        return ModelTaskCreationResult(
            task=self._snapshot(task),
            idempotent_replay=False,
        )

    def get_task(self, *, task_id: int) -> ModelExecutionTaskSnapshot:
        with self._session_factory() as db:
            task = db.get(ModelExecutionTask, task_id)
            if task is None:
                raise ModelExecutionTaskNotFoundError("模型任务不存在")
            return self._snapshot(task)

    def lease_task(
        self,
        *,
        task_id: int,
        worker_id: str,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> ModelExecutionTaskSnapshot | None:
        """按任务 ID 领取；不允许从同类型队列误领其他业务请求。"""

        with self._session_factory() as db, db.begin():
            return self.lease_task_in_transaction(
                db=db,
                task_id=task_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                now=now,
            )

    def lease_task_in_transaction(
        self,
        *,
        db: Session,
        task_id: int,
        worker_id: str,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> ModelExecutionTaskSnapshot | None:
        stored_worker_id, leased_at = self._validated_lease_request(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            now=now,
        )
        task = self._locked_task(db, task_id=task_id)
        if not self._is_lease_eligible(task, now=leased_at):
            return None
        self._apply_lease(
            task,
            worker_id=stored_worker_id,
            leased_at=leased_at,
            lease_seconds=lease_seconds,
        )
        db.flush()
        db.refresh(task)
        return self._snapshot(task)

    def lease_next(
        self,
        *,
        worker_id: str,
        task_types: tuple[ModelTaskType, ...] | None = None,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> ModelExecutionTaskSnapshot | None:
        """以行锁领取一个到期任务；过期租约可以被其他 Worker 接管。"""

        stored_worker_id, leased_at = self._validated_lease_request(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            now=now,
        )
        if task_types is not None and (
            not task_types
            or any(not isinstance(item, ModelTaskType) for item in task_types)
        ):
            raise InvalidModelExecutionTaskError("模型任务类型过滤条件无效")

        eligible = or_(
            ModelExecutionTask.status == ModelTaskStatus.PENDING,
            and_(
                ModelExecutionTask.status == ModelTaskStatus.RETRY_WAIT,
                ModelExecutionTask.next_retry_at <= leased_at,
            ),
            and_(
                ModelExecutionTask.status == ModelTaskStatus.RUNNING,
                ModelExecutionTask.lease_expires_at <= leased_at,
            ),
        )
        with self._session_factory() as db, db.begin():
            statement = (
                select(ModelExecutionTask)
                .where(eligible)
                .order_by(
                    case(
                        (ModelExecutionTask.status == ModelTaskStatus.PENDING, 0),
                        (ModelExecutionTask.status == ModelTaskStatus.RETRY_WAIT, 1),
                        else_=2,
                    ),
                    ModelExecutionTask.id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if task_types is not None:
                statement = statement.where(
                    ModelExecutionTask.task_type.in_(task_types)
                )
            task = db.scalar(statement)
            if task is None:
                return None

            self._apply_lease(
                task,
                worker_id=stored_worker_id,
                leased_at=leased_at,
                lease_seconds=lease_seconds,
            )
            db.flush()
            db.refresh(task)
            return self._snapshot(task)

    def complete_success(
        self,
        *,
        task_id: int,
        lease_token: str,
        result_snapshot: dict[str, object],
        usage: ModelUsage,
        now: datetime | None = None,
    ) -> ModelExecutionTaskSnapshot:
        """持有有效租约时保存模型输出和用量；输出本身不是业务授权。"""

        with self._session_factory() as db, db.begin():
            return self.complete_success_in_transaction(
                db=db,
                task_id=task_id,
                lease_token=lease_token,
                result_snapshot=result_snapshot,
                usage=usage,
                now=now,
            )

    def complete_success_in_transaction(
        self,
        *,
        db: Session,
        task_id: int,
        lease_token: str,
        result_snapshot: dict[str, object],
        usage: ModelUsage,
        now: datetime | None = None,
    ) -> ModelExecutionTaskSnapshot:
        stored_result, _ = self._validated_json_object(result_snapshot)
        stored_usage = self._validated_usage(usage)
        completed_at = self._database_datetime(now or datetime.now(UTC))
        task = self._locked_task(db, task_id=task_id)
        self._require_active_lease(
            task,
            lease_token=lease_token,
            now=completed_at,
        )
        task.status = ModelTaskStatus.SUCCEEDED
        task.result_snapshot = stored_result
        task.model_provider = stored_usage.provider
        task.model_name = stored_usage.model_name
        task.input_tokens = stored_usage.input_tokens
        task.output_tokens = stored_usage.output_tokens
        task.total_tokens = stored_usage.total_tokens
        task.estimated_cost = stored_usage.estimated_cost
        task.last_error_category = None
        task.last_error_message = None
        task.completed_at = completed_at
        self._clear_lease(task)
        db.flush()
        db.refresh(task)
        return self._snapshot(task)

    def defer_retry(
        self,
        *,
        task_id: int,
        lease_token: str,
        error_category: ModelTaskErrorCategory,
        next_retry_at: datetime,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> ModelExecutionTaskSnapshot:
        """记录一次可重试失败；退避算法和最大次数在阶段四接入。"""

        with self._session_factory() as db, db.begin():
            return self.defer_retry_in_transaction(
                db=db,
                task_id=task_id,
                lease_token=lease_token,
                error_category=error_category,
                next_retry_at=next_retry_at,
                error_message=error_message,
                now=now,
            )

    def defer_retry_in_transaction(
        self,
        *,
        db: Session,
        task_id: int,
        lease_token: str,
        error_category: ModelTaskErrorCategory,
        next_retry_at: datetime,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> ModelExecutionTaskSnapshot:
        failed_at = self._database_datetime(now or datetime.now(UTC))
        stored_retry_at = self._database_datetime(next_retry_at)
        if stored_retry_at <= failed_at:
            raise InvalidModelExecutionTaskError("下次重试时间必须晚于当前时间")
        stored_error = self._validated_error_category(error_category)
        stored_message = self._safe_error_message(error_message)
        task = self._locked_task(db, task_id=task_id)
        self._require_active_lease(
            task,
            lease_token=lease_token,
            now=failed_at,
        )
        task.status = ModelTaskStatus.RETRY_WAIT
        task.next_retry_at = stored_retry_at
        task.last_error_category = stored_error
        task.last_error_message = stored_message
        task.completed_at = None
        self._clear_lease(task, clear_retry_at=False)
        db.flush()
        db.refresh(task)
        return self._snapshot(task)

    def fail_task(
        self,
        *,
        task_id: int,
        lease_token: str,
        error_category: ModelTaskErrorCategory,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> ModelExecutionTaskSnapshot:
        """将持有租约的任务置为不可自动重试的失败终态。"""

        return self._complete_error(
            task_id=task_id,
            lease_token=lease_token,
            target_status=ModelTaskStatus.FAILED,
            error_category=error_category,
            error_message=error_message,
            now=now,
        )

    def mark_stale(
        self,
        *,
        task_id: int,
        lease_token: str,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> ModelExecutionTaskSnapshot:
        """标记版本复核失败的迟到结果，禁止其成为业务事实。"""

        return self._complete_error(
            task_id=task_id,
            lease_token=lease_token,
            target_status=ModelTaskStatus.STALE,
            error_category=ModelTaskErrorCategory.BUSINESS_CONFLICT,
            error_message=error_message,
            now=now,
        )

    def mark_stale_in_transaction(
        self,
        *,
        db: Session,
        task_id: int,
        lease_token: str,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> ModelExecutionTaskSnapshot:
        return self._complete_error_in_transaction(
            db=db,
            task_id=task_id,
            lease_token=lease_token,
            target_status=ModelTaskStatus.STALE,
            error_category=ModelTaskErrorCategory.BUSINESS_CONFLICT,
            error_message=error_message,
            now=now,
        )

    def require_active_lease_in_transaction(
        self,
        *,
        db: Session,
        task_id: int,
        lease_token: str,
        now: datetime | None = None,
    ) -> ModelExecutionTaskSnapshot:
        checked_at = self._database_datetime(now or datetime.now(UTC))
        task = self._locked_task(db, task_id=task_id)
        self._require_active_lease(
            task,
            lease_token=lease_token,
            now=checked_at,
        )
        return self._snapshot(task)

    def _complete_error(
        self,
        *,
        task_id: int,
        lease_token: str,
        target_status: ModelTaskStatus,
        error_category: ModelTaskErrorCategory,
        error_message: str | None,
        now: datetime | None,
    ) -> ModelExecutionTaskSnapshot:
        with self._session_factory() as db, db.begin():
            return self._complete_error_in_transaction(
                db=db,
                task_id=task_id,
                lease_token=lease_token,
                target_status=target_status,
                error_category=error_category,
                error_message=error_message,
                now=now,
            )

    def _complete_error_in_transaction(
        self,
        *,
        db: Session,
        task_id: int,
        lease_token: str,
        target_status: ModelTaskStatus,
        error_category: ModelTaskErrorCategory,
        error_message: str | None,
        now: datetime | None,
    ) -> ModelExecutionTaskSnapshot:
        completed_at = self._database_datetime(now or datetime.now(UTC))
        stored_error = self._validated_error_category(error_category)
        stored_message = self._safe_error_message(error_message)
        task = self._locked_task(db, task_id=task_id)
        self._require_active_lease(
            task,
            lease_token=lease_token,
            now=completed_at,
        )
        task.status = target_status
        task.last_error_category = stored_error
        task.last_error_message = stored_message
        task.completed_at = completed_at
        self._clear_lease(task)
        db.flush()
        db.refresh(task)
        return self._snapshot(task)

    @staticmethod
    def _capture_references(
        db: Session,
        *,
        task_type: ModelTaskType,
        session_id: int,
        offer_id: int | None,
        approval_id: int | None,
    ) -> tuple[int, int]:
        negotiation = db.get(NegotiationSession, session_id, with_for_update=True)
        if negotiation is None:
            raise InvalidModelExecutionTaskError("模型任务关联的协商会话不存在")
        policy = db.scalar(
            select(SellerPolicy)
            .where(SellerPolicy.product_id == negotiation.product_id)
            .with_for_update()
        )
        if policy is None:
            raise InvalidModelExecutionTaskError("模型任务关联的卖家规则不存在")

        offer = None
        if offer_id is not None:
            offer = db.get(Offer, offer_id, with_for_update=True)
            if offer is None or offer.session_id != session_id:
                raise InvalidModelExecutionTaskError("模型任务关联的报价不属于当前会话")

        approval = None
        if approval_id is not None:
            approval = db.get(ApprovalRequest, approval_id, with_for_update=True)
            if approval is None or approval.session_id != session_id:
                raise InvalidModelExecutionTaskError("模型任务关联的审批不属于当前会话")

        if task_type is ModelTaskType.CHAT_DECISION:
            if approval is not None:
                raise InvalidModelExecutionTaskError("聊天决策任务不能绑定审批记录")
            stored_policy_version = policy.version
        else:
            if approval is None or offer is None or approval.offer_id != offer.id:
                raise InvalidModelExecutionTaskError("审批通知任务必须绑定一致的审批和报价")
            if approval.status not in {
                ApprovalStatus.APPROVED,
                ApprovalStatus.REJECTED,
            } or not approval.followup_request_id:
                raise InvalidModelExecutionTaskError("审批通知任务只能为已审核记录创建")
            # 审批自身保留授权时的 policy_version；任务保存创建时的当前版本，
            # 便于模型返回后识别调用期间发生的再次变更。
            stored_policy_version = policy.version

        return negotiation.version, stored_policy_version

    @staticmethod
    def _by_business_key(
        db: Session,
        *,
        task_type: ModelTaskType,
        business_key: str,
    ) -> ModelExecutionTask | None:
        return db.scalar(
            select(ModelExecutionTask).where(
                ModelExecutionTask.task_type == task_type,
                ModelExecutionTask.business_key == business_key,
            )
        )

    def _existing_creation_result(
        self,
        task: ModelExecutionTask,
        *,
        session_id: int,
        offer_id: int | None,
        approval_id: int | None,
        snapshot_schema_version: int,
        input_hash: str,
    ) -> ModelTaskCreationResult:
        if (
            task.session_id != session_id
            or task.offer_id != offer_id
            or task.approval_id != approval_id
            or task.snapshot_schema_version != snapshot_schema_version
            or task.input_snapshot_hash != input_hash
        ):
            raise ModelExecutionTaskConflictError(
                "相同模型任务幂等键不能用于不同输入或业务对象"
            )
        return ModelTaskCreationResult(
            task=self._snapshot(task),
            idempotent_replay=True,
        )

    def _validated_lease_request(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None,
    ) -> tuple[str, datetime]:
        stored_worker_id = self._validated_identifier(
            worker_id,
            field_name="Worker 标识",
            max_length=100,
        )
        if not 1 <= lease_seconds <= 3600:
            raise InvalidModelExecutionTaskError("租约时长必须在 1 到 3600 秒之间")
        return stored_worker_id, self._database_datetime(now or datetime.now(UTC))

    @staticmethod
    def _is_lease_eligible(
        task: ModelExecutionTask,
        *,
        now: datetime,
    ) -> bool:
        if task.status is ModelTaskStatus.PENDING:
            return True
        if task.status is ModelTaskStatus.RETRY_WAIT:
            return task.next_retry_at is not None and task.next_retry_at <= now
        return (
            task.status is ModelTaskStatus.RUNNING
            and task.lease_expires_at is not None
            and task.lease_expires_at <= now
        )

    @staticmethod
    def _apply_lease(
        task: ModelExecutionTask,
        *,
        worker_id: str,
        leased_at: datetime,
        lease_seconds: int,
    ) -> None:
        task.status = ModelTaskStatus.RUNNING
        task.attempt_count += 1
        task.next_retry_at = None
        task.lease_owner = worker_id
        task.lease_token = uuid4().hex
        task.lease_expires_at = leased_at + timedelta(seconds=lease_seconds)
        if task.started_at is None:
            task.started_at = leased_at

    @staticmethod
    def _locked_task(db: Session, *, task_id: int) -> ModelExecutionTask:
        task = db.get(ModelExecutionTask, task_id, with_for_update=True)
        if task is None:
            raise ModelExecutionTaskNotFoundError("模型任务不存在")
        return task

    @staticmethod
    def _require_active_lease(
        task: ModelExecutionTask,
        *,
        lease_token: str,
        now: datetime,
    ) -> None:
        if (
            task.status is not ModelTaskStatus.RUNNING
            or not lease_token
            or task.lease_token != lease_token
            or task.lease_expires_at is None
            or task.lease_expires_at <= now
        ):
            raise ModelExecutionTaskLeaseError("模型任务租约无效或已经过期")

    @staticmethod
    def _clear_lease(
        task: ModelExecutionTask,
        *,
        clear_retry_at: bool = True,
    ) -> None:
        task.lease_owner = None
        task.lease_token = None
        task.lease_expires_at = None
        if clear_retry_at:
            task.next_retry_at = None

    @classmethod
    def _validated_json_object(
        cls,
        value: dict[str, object],
    ) -> tuple[dict[str, object], str]:
        if not isinstance(value, dict):
            raise InvalidModelExecutionTaskError("模型任务快照必须是 JSON 对象")
        try:
            canonical = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            stored = json.loads(canonical)
        except (TypeError, ValueError) as exc:
            raise InvalidModelExecutionTaskError("模型任务快照必须可以安全序列化") from exc
        return stored, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _validated_identifier(
        cls,
        value: str,
        *,
        field_name: str,
        max_length: int,
    ) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > max_length
            or _IDENTIFIER_PATTERN.fullmatch(value) is None
        ):
            raise InvalidModelExecutionTaskError(
                f"{field_name}只能包含字母、数字、点、下划线、冒号和连字符"
            )
        return value

    @classmethod
    def _validated_usage(cls, usage: ModelUsage) -> ModelUsage:
        if not isinstance(usage, ModelUsage):
            raise InvalidModelExecutionTaskError("模型用量记录无效")
        provider = usage.provider.strip()
        model_name = usage.model_name.strip()
        if not provider or len(provider) > 50:
            raise InvalidModelExecutionTaskError("模型提供商标识无效")
        if not model_name or len(model_name) > 100:
            raise InvalidModelExecutionTaskError("模型名称无效")
        for value in (usage.input_tokens, usage.output_tokens, usage.total_tokens):
            if value is not None and value < 0:
                raise InvalidModelExecutionTaskError("模型 Token 用量不能为负数")
        if usage.estimated_cost is not None and usage.estimated_cost < 0:
            raise InvalidModelExecutionTaskError("模型估算成本不能为负数")
        return ModelUsage(
            provider=provider,
            model_name=model_name,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            estimated_cost=usage.estimated_cost,
        )

    @staticmethod
    def _validated_error_category(
        value: ModelTaskErrorCategory,
    ) -> ModelTaskErrorCategory:
        if not isinstance(value, ModelTaskErrorCategory):
            raise InvalidModelExecutionTaskError("模型任务错误分类无效")
        return value

    @staticmethod
    def _safe_error_message(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized[:500] or None

    @staticmethod
    def _database_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    @staticmethod
    def _snapshot(task: ModelExecutionTask) -> ModelExecutionTaskSnapshot:
        return ModelExecutionTaskSnapshot(
            id=task.id,
            task_type=task.task_type,
            business_key=task.business_key,
            session_id=task.session_id,
            offer_id=task.offer_id,
            approval_id=task.approval_id,
            snapshot_schema_version=task.snapshot_schema_version,
            session_version=task.session_version,
            policy_version=task.policy_version,
            input_snapshot=deepcopy(task.input_snapshot),
            input_snapshot_hash=task.input_snapshot_hash,
            status=task.status,
            attempt_count=task.attempt_count,
            next_retry_at=task.next_retry_at,
            lease_owner=task.lease_owner,
            lease_token=task.lease_token,
            lease_expires_at=task.lease_expires_at,
            last_error_category=task.last_error_category,
            last_error_message=task.last_error_message,
            model_provider=task.model_provider,
            model_name=task.model_name,
            input_tokens=task.input_tokens,
            output_tokens=task.output_tokens,
            total_tokens=task.total_tokens,
            estimated_cost=task.estimated_cost,
            result_snapshot=deepcopy(task.result_snapshot),
            started_at=task.started_at,
            completed_at=task.completed_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
