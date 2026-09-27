from typing import Annotated, Never

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.dependencies import SessionFactoryDependency, get_current_seller
from app.db.models import NegotiationStatus
from app.schemas.negotiation import (
    SellerNegotiationDetailResponse,
    SellerNegotiationListResponse,
    SellerNegotiationSummaryResponse,
)
from app.services.auth_service import SellerPrincipal
from app.services.errors import NegotiationNotFoundError, ServiceError
from app.services.seller_negotiation_service import SellerNegotiationService

router = APIRouter(prefix="/seller/negotiations", tags=["seller-negotiations"])
CurrentSeller = Annotated[SellerPrincipal, Depends(get_current_seller)]


@router.get("", response_model=SellerNegotiationListResponse)
def list_seller_negotiations(
    seller: CurrentSeller,
    session_factory: SessionFactoryDependency,
    negotiation_status: Annotated[
        NegotiationStatus | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> SellerNegotiationListResponse:
    negotiations = SellerNegotiationService(session_factory).list_for_seller(
        seller_id=seller.id,
        negotiation_status=negotiation_status,
        limit=limit,
    )
    return SellerNegotiationListResponse(
        negotiations=[
            SellerNegotiationSummaryResponse.model_validate(item)
            for item in negotiations
        ]
    )


@router.get("/{session_id}", response_model=SellerNegotiationDetailResponse)
def get_seller_negotiation(
    session_id: int,
    seller: CurrentSeller,
    session_factory: SessionFactoryDependency,
) -> SellerNegotiationDetailResponse:
    try:
        negotiation = SellerNegotiationService(session_factory).get_for_seller(
            seller_id=seller.id,
            session_id=session_id,
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    return SellerNegotiationDetailResponse.model_validate(negotiation)


def _raise_http_error(error: ServiceError) -> Never:
    code = (
        status.HTTP_404_NOT_FOUND
        if isinstance(error, NegotiationNotFoundError)
        else status.HTTP_400_BAD_REQUEST
    )
    raise HTTPException(status_code=code, detail=str(error)) from error
