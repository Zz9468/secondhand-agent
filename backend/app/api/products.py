from typing import Annotated, Never

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.dependencies import SessionFactoryDependency, get_current_seller
from app.schemas.product import (
    PolicyUpdateRequest,
    ProductCreateRequest,
    ProductUpdateRequest,
    PublicProductListResponse,
    PublicProductResponse,
    SellerProductListResponse,
    SellerProductResponse,
)
from app.services.auth_service import SellerPrincipal
from app.services.errors import (
    PolicyVersionConflictError,
    PricingPolicyNotFoundError,
    ProductNotFoundError,
    ServiceError,
)
from app.services.product_service import (
    PolicyUpdateData,
    ProductCreateData,
    ProductService,
    ProductUpdateData,
)

public_router = APIRouter(tags=["products"])
seller_router = APIRouter(prefix="/seller/products", tags=["seller-products"])
CurrentSeller = Annotated[SellerPrincipal, Depends(get_current_seller)]


@public_router.get("/products", response_model=PublicProductListResponse)
def list_public_products(
    session_factory: SessionFactoryDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PublicProductListResponse:
    products = ProductService(session_factory).list_public(offset=offset, limit=limit)
    return PublicProductListResponse(
        products=[PublicProductResponse.model_validate(product) for product in products]
    )


@public_router.get("/products/{product_id}", response_model=PublicProductResponse)
def get_public_product(
    product_id: int,
    session_factory: SessionFactoryDependency,
) -> PublicProductResponse:
    try:
        product = ProductService(session_factory).get_public(product_id=product_id)
    except ServiceError as exc:
        _raise_http_error(exc)
    return PublicProductResponse.model_validate(product)


@public_router.post(
    "/products",
    response_model=SellerProductResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_product(
    payload: ProductCreateRequest,
    seller: CurrentSeller,
    session_factory: SessionFactoryDependency,
) -> SellerProductResponse:
    policy = payload.policy
    product = ProductService(session_factory).create_for_seller(
        seller_id=seller.id,
        data=ProductCreateData(
            title=payload.title,
            description=payload.description,
            listed_price=payload.listed_price,
            status=payload.status,
            minimum_net_price=policy.minimum_net_price,
            auto_accept_threshold=policy.auto_accept_threshold,
            negotiation_style=policy.negotiation_style,
            max_rounds=policy.max_rounds,
        ),
    )
    return SellerProductResponse.model_validate(product)


@seller_router.get("", response_model=SellerProductListResponse)
def list_seller_products(
    seller: CurrentSeller,
    session_factory: SessionFactoryDependency,
) -> SellerProductListResponse:
    products = ProductService(session_factory).list_owned(seller_id=seller.id)
    return SellerProductListResponse(
        products=[SellerProductResponse.model_validate(product) for product in products]
    )


@seller_router.get("/{product_id}", response_model=SellerProductResponse)
def get_seller_product(
    product_id: int,
    seller: CurrentSeller,
    session_factory: SessionFactoryDependency,
) -> SellerProductResponse:
    try:
        product = ProductService(session_factory).get_owned_detail(
            product_id=product_id,
            seller_id=seller.id,
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    return SellerProductResponse.model_validate(product)


@seller_router.put("/{product_id}", response_model=SellerProductResponse)
def update_seller_product(
    product_id: int,
    payload: ProductUpdateRequest,
    seller: CurrentSeller,
    session_factory: SessionFactoryDependency,
) -> SellerProductResponse:
    try:
        product = ProductService(session_factory).update_owned_product(
            product_id=product_id,
            seller_id=seller.id,
            data=ProductUpdateData(
                title=payload.title,
                description=payload.description,
                listed_price=payload.listed_price,
                status=payload.status,
            ),
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    return SellerProductResponse.model_validate(product)


@seller_router.put("/{product_id}/policy", response_model=SellerProductResponse)
def update_seller_policy(
    product_id: int,
    payload: PolicyUpdateRequest,
    seller: CurrentSeller,
    session_factory: SessionFactoryDependency,
) -> SellerProductResponse:
    try:
        product = ProductService(session_factory).update_owned_policy(
            product_id=product_id,
            seller_id=seller.id,
            data=PolicyUpdateData(
                minimum_net_price=payload.minimum_net_price,
                auto_accept_threshold=payload.auto_accept_threshold,
                negotiation_style=payload.negotiation_style,
                max_rounds=payload.max_rounds,
                expected_version=payload.expected_version,
            ),
        )
    except ServiceError as exc:
        _raise_http_error(exc)
    return SellerProductResponse.model_validate(product)


def _raise_http_error(error: ServiceError) -> Never:
    if isinstance(error, ProductNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, PolicyVersionConflictError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(error, PricingPolicyNotFoundError):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        code = status.HTTP_400_BAD_REQUEST
    raise HTTPException(status_code=code, detail=str(error)) from error
