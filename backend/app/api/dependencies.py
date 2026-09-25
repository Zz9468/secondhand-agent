from functools import lru_cache

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, sessionmaker

from app.agent.decision_provider import DecisionProvider, LangChainDecisionProvider
from app.agent.model_factory import ModelConfigurationError, QwenChatModelFactory
from app.core.config import get_settings
from app.db.session import get_session_factory


def get_session_factory_dependency() -> sessionmaker[Session]:
    return get_session_factory()


@lru_cache
def _get_configured_decision_provider() -> DecisionProvider:
    model = QwenChatModelFactory().create(get_settings())
    return LangChainDecisionProvider(model)


def get_decision_provider() -> DecisionProvider:
    try:
        return _get_configured_decision_provider()
    except ModelConfigurationError as exc:
        # 对外只说明配置缺失，不回显可能包含服务地址的配置细节。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="model service is not configured",
        ) from exc
