"""Owned public display endpoints and seller evidence maintenance."""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import Field, field_validator

from app.api.dependencies import CurrentUser, SessionFactoryDependency
from app.runtime.contracts import Conflict, utcnow
from app.semantic.contracts import StrictModel
from app.semantic.maintenance import EvidenceMaintenance
from app.semantic.presentation import refresh_context
from app.services.notification_service import NotificationService

router = APIRouter(tags=["semantic-chat"])


class DisplayRequest(StrictModel):
    context_id: str = Field(min_length=1, max_length=32)
    client_view_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=64)


class RefreshRequest(StrictModel):
    context_id: str = Field(min_length=1, max_length=32)
    request_id: str = Field(min_length=1, max_length=64)


class FactRequest(StrictModel):
    expected_version: int = Field(gt=0)
    value: str | list[str] | None


class RuleRequest(StrictModel):
    expected_version: int = Field(gt=0)
    rule_id: str | None = Field(default=None, max_length=32)
    region: str = Field(min_length=1, max_length=80)
    shipping_cost: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    discount: Decimal = Field(default=Decimal(0), ge=0, max_digits=12, decimal_places=2)
    active: bool = True
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def normalize_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("规则失效时间必须带时区")
        return value.astimezone(UTC).replace(tzinfo=None)


def conflict(call):
    try:
        return call()
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/negotiations/{session_id}/display-acks")
def display(
    session_id: int, payload: DisplayRequest, user: CurrentUser, factory: SessionFactoryDependency
):
    return conflict(
        lambda: {
            "display_receipt_id": NotificationService(factory).display(
                session_id, user.id, payload.context_id, payload.client_view_id, payload.request_id
            )
        }
    )


@router.post("/negotiations/{session_id}/interaction-contexts/refresh")
def refresh(
    session_id: int, payload: RefreshRequest, user: CurrentUser, factory: SessionFactoryDependency
):
    return conflict(
        lambda: refresh_context(
            factory, session_id, user.id, payload.context_id, payload.request_id, utcnow()
        )
    )


@router.get("/seller/products/{product_id}/evidence")
def evidence(product_id: int, user: CurrentUser, factory: SessionFactoryDependency):
    return conflict(lambda: EvidenceMaintenance(factory).inspect(product_id, user.id))


@router.put("/seller/products/{product_id}/facts/{attribute}")
def fact(
    product_id: int,
    attribute: str,
    payload: FactRequest,
    user: CurrentUser,
    factory: SessionFactoryDependency,
):
    return conflict(
        lambda: EvidenceMaintenance(factory).set_fact(
            product_id, user.id, attribute, payload.value, payload.expected_version
        )
    )


@router.put("/seller/products/{product_id}/shipping-rules")
def rule(
    product_id: int, payload: RuleRequest, user: CurrentUser, factory: SessionFactoryDependency
):
    return conflict(lambda: EvidenceMaintenance(factory).set_rule(product_id, user.id, payload))


@router.post("/seller/products/{product_id}/verifications/recheck")
def recheck(product_id: int, user: CurrentUser, factory: SessionFactoryDependency):
    return conflict(
        lambda: EvidenceMaintenance(factory).recheck(product_id, user.id) or {"checked": True}
    )
