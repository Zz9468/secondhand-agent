from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.buyer_negotiations import router as buyer_negotiations_router
from app.api.graph_negotiations import router as negotiations_router
from app.api.graph_recovery import router as graph_recovery_router
from app.api.graph_runs import router as graph_runs_router
from app.api.health import router as health_router
from app.api.products import public_router as products_router
from app.api.products import seller_router as seller_products_router
from app.api.seller_approvals import router as seller_approvals_router
from app.api.seller_negotiations import router as seller_negotiations_router
from app.api.sellers import router as sellers_router
from app.api.semantic import router as semantic_router
from app.core.config import get_settings
from app.observability.middleware import RequestObservationMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.graph_require_services:
        from app.runtime.readiness import validate_targets

        validate_targets(settings)
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="SecondHand Agent API",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(RequestObservationMiddleware)
    application.include_router(health_router, prefix=settings.api_prefix)
    application.include_router(graph_runs_router, prefix=settings.api_prefix)
    application.include_router(graph_recovery_router, prefix=settings.api_prefix)
    application.include_router(semantic_router, prefix=settings.api_prefix)
    application.include_router(auth_router, prefix=settings.api_prefix)
    application.include_router(products_router, prefix=settings.api_prefix)
    application.include_router(sellers_router, prefix=settings.api_prefix)
    application.include_router(seller_products_router, prefix=settings.api_prefix)
    application.include_router(seller_approvals_router, prefix=settings.api_prefix)
    application.include_router(seller_negotiations_router, prefix=settings.api_prefix)
    application.include_router(buyer_negotiations_router, prefix=settings.api_prefix)
    application.include_router(negotiations_router, prefix=settings.api_prefix)
    return application


app = create_app()
