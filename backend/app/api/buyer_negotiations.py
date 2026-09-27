from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentUser, SessionFactoryDependency
from app.schemas.negotiation import (
    BuyerNegotiationListResponse,
    BuyerNegotiationSummaryResponse,
)
from app.services.buyer_negotiation_service import BuyerNegotiationService

router = APIRouter(prefix="/buyer/negotiations", tags=["buyer-negotiations"])


@router.get("", response_model=BuyerNegotiationListResponse)
def list_buyer_negotiations(
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> BuyerNegotiationListResponse:
    negotiations = BuyerNegotiationService(session_factory).list_for_buyer(
        buyer_id=user.id,
        limit=limit,
    )
    return BuyerNegotiationListResponse(
        negotiations=[
            BuyerNegotiationSummaryResponse.model_validate(item)
            for item in negotiations
        ]
    )
