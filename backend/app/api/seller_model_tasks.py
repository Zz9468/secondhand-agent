from typing import Annotated, Never

from fastapi import APIRouter, HTTPException, Query, status

from app.api.dependencies import CurrentUser, SessionFactoryDependency
from app.db.models import ModelTaskStatus
from app.observability.service import ObservabilityEventInput, ObservabilityService
from app.schemas.model_task import (
    ModelTaskManualActionRequest,
    SellerModelTaskListResponse,
    SellerModelTaskResponse,
)
from app.services.errors import (
    ModelExecutionTaskConflictError,
    ModelExecutionTaskNotFoundError,
    ServiceError,
)
from app.services.model_task_recovery_service import ModelTaskRecoveryService

router = APIRouter(prefix="/seller/model-tasks", tags=["seller-model-tasks"])


@router.get("", response_model=SellerModelTaskListResponse)
def list_seller_model_tasks(
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
    task_status: Annotated[ModelTaskStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> SellerModelTaskListResponse:
    tasks = ModelTaskRecoveryService(session_factory).list_for_seller(
        seller_id=user.id,
        statuses=(task_status,) if task_status is not None else None,
        limit=limit,
    )
    return SellerModelTaskListResponse(
        tasks=[SellerModelTaskResponse.model_validate(item) for item in tasks]
    )


@router.post("/{task_id}/retry", response_model=SellerModelTaskResponse)
def retry_seller_model_task(
    task_id: int,
    payload: ModelTaskManualActionRequest,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
) -> SellerModelTaskResponse:
    try:
        task = ModelTaskRecoveryService(session_factory).retry_failed(
            seller_id=user.id,
            task_id=task_id,
            reason=payload.reason,
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    ObservabilityService(session_factory).record(
        ObservabilityEventInput(
            event_type="MODEL_TASK_MANUAL_ACTION",
            action="RETRY_MODEL_TASK",
            outcome="SUCCESS",
            session_id=task.session_id,
            model_task_id=task.id,
            approval_id=task.approval_id,
            offer_id=task.offer_id,
            attempt_count=task.attempt_count,
            attributes={
                "manual_action": "RETRY",
                "task_status": task.status.value,
                "task_type": task.task_type.value,
            },
        )
    )
    return SellerModelTaskResponse.model_validate(task)


@router.post("/{task_id}/terminate", response_model=SellerModelTaskResponse)
def terminate_seller_model_task(
    task_id: int,
    payload: ModelTaskManualActionRequest,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
) -> SellerModelTaskResponse:
    try:
        task = ModelTaskRecoveryService(session_factory).terminate(
            seller_id=user.id,
            task_id=task_id,
            reason=payload.reason,
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    ObservabilityService(session_factory).record(
        ObservabilityEventInput(
            event_type="MODEL_TASK_MANUAL_ACTION",
            action="TERMINATE_MODEL_TASK",
            outcome="SUCCESS",
            session_id=task.session_id,
            model_task_id=task.id,
            approval_id=task.approval_id,
            offer_id=task.offer_id,
            attempt_count=task.attempt_count,
            attributes={
                "manual_action": "TERMINATE",
                "task_status": task.status.value,
                "task_type": task.task_type.value,
            },
        )
    )
    return SellerModelTaskResponse.model_validate(task)


def _raise_http_error(error: ServiceError) -> Never:
    if isinstance(error, ModelExecutionTaskNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, ModelExecutionTaskConflictError):
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_400_BAD_REQUEST
    raise HTTPException(status_code=code, detail=str(error)) from error
