from typing import Annotated, Never

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.api.dependencies import SessionFactoryDependency
from app.schemas.product import PublicProductListResponse, PublicProductResponse
from app.schemas.seller import PublicSellerListResponse, PublicSellerResponse
from app.services.errors import SellerNotFoundError, ServiceError
from app.services.product_service import ProductService
from app.services.seller_service import SellerService

router = APIRouter(prefix="/sellers", tags=["sellers"])


@router.get("", response_model=PublicSellerListResponse)
def list_public_sellers(
    session_factory: SessionFactoryDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PublicSellerListResponse:
    sellers = SellerService(session_factory).list_public(offset=offset, limit=limit)
    return PublicSellerListResponse(
        sellers=[PublicSellerResponse.model_validate(seller) for seller in sellers]
    )


@router.get("/{seller_id}", response_model=PublicSellerResponse)
def get_public_seller(
    seller_id: Annotated[str, Path(min_length=1, max_length=64)],
    session_factory: SessionFactoryDependency,
) -> PublicSellerResponse:
    try:
        seller = SellerService(session_factory).get_public(seller_id=seller_id)
    except ServiceError as exc:
        _raise_http_error(exc)
    return PublicSellerResponse.model_validate(seller)


@router.get("/{seller_id}/products", response_model=PublicProductListResponse)
def list_public_seller_products(
    seller_id: Annotated[str, Path(min_length=1, max_length=64)],
    session_factory: SessionFactoryDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PublicProductListResponse:
    try:
        SellerService(session_factory).get_public(seller_id=seller_id)
    except ServiceError as exc:
        _raise_http_error(exc)
    products = ProductService(session_factory).list_public(
        seller_id=seller_id,
        offset=offset,
        limit=limit,
    )
    return PublicProductListResponse(
        products=[PublicProductResponse.model_validate(product) for product in products]
    )


def _raise_http_error(error: ServiceError) -> Never:
    if isinstance(error, SellerNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    else:
        code = status.HTTP_400_BAD_REQUEST
    raise HTTPException(status_code=code, detail=str(error)) from error
