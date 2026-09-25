from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.negotiations import router as negotiations_router
from app.api.products import public_router as products_router
from app.api.products import seller_router as seller_products_router
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
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
    application.include_router(health_router, prefix=settings.api_prefix)
    application.include_router(auth_router, prefix=settings.api_prefix)
    application.include_router(products_router, prefix=settings.api_prefix)
    application.include_router(seller_products_router, prefix=settings.api_prefix)
    application.include_router(negotiations_router, prefix=settings.api_prefix)
    return application


app = create_app()
