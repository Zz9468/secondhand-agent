import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少数据库连接配置时应立即失败，不能回退到固定口令。"""

    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)
