"""P1 只公开受身份保护的运行查询，不公开固定类型化测试入口。"""

from fastapi import APIRouter, HTTPException

from app.api.dependencies import CurrentUser, SessionFactoryDependency
from app.runtime.contracts import Conflict
from app.services.graph_status_service import GraphStatusService

router = APIRouter(prefix="/negotiations", tags=["graph-runs"])


@router.get("/{session_id}/requests/{request_id}")
def get_graph_request(
    session_id: int,
    request_id: str,
    user: CurrentUser,
    session_factory: SessionFactoryDependency,
):
    try:
        return GraphStatusService(session_factory).get_request(session_id, user.id, request_id)
    except Conflict as exc:
        raise HTTPException(status_code=404, detail="请求不存在或无权访问") from exc


@router.get("/{session_id}/runs/{run_id}")
def get_graph_run(
    session_id: int, run_id: str, user: CurrentUser, session_factory: SessionFactoryDependency
):
    try:
        return GraphStatusService(session_factory).get(session_id, user.id, run_id)
    except Conflict as exc:
        raise HTTPException(status_code=404, detail="运行不存在或无权访问") from exc
