import pytest
from pydantic import SecretStr

from app.core.config import Settings
from evaluation import cli


def test_real_model_requires_explicit_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(_env_file=None, database_url="sqlite://"),
    )

    with pytest.raises(SystemExit, match="--allow-real-model"):
        cli.main(["--model", "qwen"])


def test_real_model_requires_cost_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            database_url="sqlite://",
            model_base_url="https://example.invalid/v1",
            model_api_key=SecretStr("synthetic-test-key"),
        ),
    )

    with pytest.raises(SystemExit, match="--max-cost"):
        cli.main(["--model", "qwen", "--allow-real-model"])
