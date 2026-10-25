from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.dependencies import CurrentUser, SessionFactoryDependency, SettingsDependency
from app.persistence.checkpointer import ThreadBusy
from app.runtime.contracts import Conflict
from app.services.graph_recovery_service import GraphRecoveryService

router = APIRouter(prefix="/seller/graph-recovery", tags=["graph-recovery"])


class RecoveryRequest(BaseModel):
    action: str = Field(pattern="^(RETRY|TERMINATE|NOTIFY)$")
    request_id: str = Field(min_length=8, max_length=64)
    reason: str = Field(min_length=1, max_length=500)


@router.get("")
def list_recovery(
    user: CurrentUser, session_factory: SessionFactoryDependency, settings: SettingsDependency
):
    return {
        "items": GraphRecoveryService(session_factory, settings.checkpoint_database_url).list(
            user.id
        )
    }


@router.post("/{target}")
def recover(
    target: str,
    payload: RecoveryRequest,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
    settings: SettingsDependency,
):
    try:
        return GraphRecoveryService(session_factory, settings.checkpoint_database_url).act(
            user.id, target, payload.action, payload.request_id, payload.reason
        )
    except (Conflict, ThreadBusy) as exc:
        raise HTTPException(
            status_code=409, detail="恢复目标无权访问、正在运行或状态不支持操作"
        ) from exc
