from typing import Annotated, Never

from fastapi import APIRouter, HTTPException, Query, status

from app.api.dependencies import CurrentUser, SessionFactoryDependency
from app.db.models import ApprovalStatus
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

router = APIRouter(prefix="/seller/approvals", tags=["seller-approvals"])


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
        approvals=[
            SellerApprovalResponse.model_validate(approval) for approval in approvals
        ]
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
        approval = ApprovalService(session_factory).approve_request(
            seller_id=user.id,
            approval_id=approval_id,
            request_id=payload.request_id,
            comment=payload.comment,
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    return SellerApprovalResponse.model_validate(approval)


@router.post("/{approval_id}/reject", response_model=SellerApprovalResponse)
def reject_seller_approval(
    approval_id: int,
    payload: SellerApprovalDecisionRequest,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
) -> SellerApprovalResponse:
    try:
        approval = ApprovalService(session_factory).reject_request(
            seller_id=user.id,
            approval_id=approval_id,
            request_id=payload.request_id,
            comment=payload.comment,
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    return SellerApprovalResponse.model_validate(approval)


def _raise_http_error(error: ServiceError) -> Never:
    if isinstance(error, ApprovalNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, (ApprovalConflictError, ApprovalNotAuthorizedError)):
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_400_BAD_REQUEST
    raise HTTPException(status_code=code, detail=str(error)) from error
