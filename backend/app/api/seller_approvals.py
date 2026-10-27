from typing import Annotated, Never

from fastapi import APIRouter, HTTPException, Query, status

from app.api.dependencies import CurrentUser, SessionFactoryDependency
from app.db.models import ApprovalStatus
from app.observability.service import ObservabilityEventInput, ObservabilityService
from app.schemas.approval import (
    SellerApprovalDecisionRequest,
    SellerApprovalListResponse,
    SellerApprovalResponse,
)
from app.services.approval_service import ApprovalService
from app.services.errors import (
    ApprovalConflictError,
    ApprovalNotAuthorizedError,
    ApprovalNotFoundError,
    ServiceError,
)
from app.services.graph_approval_service import GraphApprovalService

router = APIRouter(prefix="/seller/approvals", tags=["seller-approvals"])


def review(factory, seller_id, approval_id, payload, approved):
    service = ApprovalService(factory)
    snapshot = service.get_for_seller(seller_id=seller_id, approval_id=approval_id)
    if snapshot.effect_status is not None:
        return GraphApprovalService(factory).review(
            seller_id=seller_id,
            approval_id=approval_id,
            request_id=payload.request_id,
            comment=payload.comment,
            approved=approved,
            expected_revision=payload.expected_effect_revision,
        )
    raise ApprovalConflictError("旧版审批需要由原版本服务排空")


@router.get("", response_model=SellerApprovalListResponse)
def list_seller_approvals(
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
    approval_status: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> SellerApprovalListResponse:
    approvals = ApprovalService(session_factory).list_for_seller(
        seller_id=user.id,
        approval_status=approval_status,
        limit=limit,
    )
    return SellerApprovalListResponse(
        approvals=[SellerApprovalResponse.model_validate(approval) for approval in approvals]
    )


@router.get("/{approval_id}", response_model=SellerApprovalResponse)
def get_seller_approval(
    approval_id: int,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
) -> SellerApprovalResponse:
    try:
        approval = ApprovalService(session_factory).get_for_seller(
            seller_id=user.id,
            approval_id=approval_id,
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    return SellerApprovalResponse.model_validate(approval)


@router.post("/{approval_id}/approve", response_model=SellerApprovalResponse)
def approve_seller_approval(
    approval_id: int,
    payload: SellerApprovalDecisionRequest,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
) -> SellerApprovalResponse:
    try:
        approval = review(session_factory, user.id, approval_id, payload, True)
    except ServiceError as exc:
        _raise_http_error(exc)
    ObservabilityService(session_factory).record(
        ObservabilityEventInput(
            event_type="APPROVAL_REVIEWED",
            action="APPROVE_REQUEST",
            outcome="SUCCESS",
            session_id=approval.session_id,
            approval_id=approval.id,
            offer_id=approval.offer_id,
            attributes={"approval_status": approval.status.value},
        )
    )
    return SellerApprovalResponse.model_validate(approval)


@router.get("/{approval_id}/requests/{request_id}")
def get_approval_request(
    approval_id: int,
    request_id: str,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
):
    try:
        return GraphApprovalService(session_factory).get_request(user.id, approval_id, request_id)
    except ServiceError as exc:
        _raise_http_error(exc)


@router.post("/{approval_id}/reject", response_model=SellerApprovalResponse)
def reject_seller_approval(
    approval_id: int,
    payload: SellerApprovalDecisionRequest,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
) -> SellerApprovalResponse:
    try:
        approval = review(session_factory, user.id, approval_id, payload, False)
    except ServiceError as exc:
        _raise_http_error(exc)
    ObservabilityService(session_factory).record(
        ObservabilityEventInput(
            event_type="APPROVAL_REVIEWED",
            action="REJECT_REQUEST",
            outcome="SUCCESS",
            session_id=approval.session_id,
            approval_id=approval.id,
            offer_id=approval.offer_id,
            attributes={"approval_status": approval.status.value},
        )
    )
    return SellerApprovalResponse.model_validate(approval)


def _raise_http_error(error: ServiceError) -> Never:
    if isinstance(error, ApprovalNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, (ApprovalConflictError, ApprovalNotAuthorizedError)):
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_400_BAD_REQUEST
    raise HTTPException(status_code=code, detail=str(error)) from error
