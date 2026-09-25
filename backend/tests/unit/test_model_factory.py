from pydantic import SecretStr

from app.agent.model_factory import ModelConfigurationError, QwenChatModelFactory
from app.core.config import Settings

TEST_DATABASE_URL = "mysql+pymysql://user:password@127.0.0.1:3306/test"


def test_qwen_factory_requires_secret_and_region_base_url() -> None:
    factory = QwenChatModelFactory()

    try:
        factory.create(
            Settings(_env_file=None, database_url=TEST_DATABASE_URL)
        )
    except ModelConfigurationError as exc:
        assert "MODEL_API_KEY" in str(exc)
    else:
        raise AssertionError("missing API key must be rejected")

    try:
        factory.create(
            Settings(
                _env_file=None,
                database_url=TEST_DATABASE_URL,
                model_api_key=SecretStr("test-key"),
            )
        )
    except ModelConfigurationError as exc:
        assert "MODEL_BASE_URL" in str(exc)
    else:
        raise AssertionError("missing base URL must be rejected")


def test_qwen_factory_builds_openai_compatible_chat_model_without_network_call() -> None:
    model = QwenChatModelFactory().create(
        Settings(
            _env_file=None,
            database_url=TEST_DATABASE_URL,
            model_api_key=SecretStr("test-key"),
            model_base_url="https://example.invalid/compatible-mode/v1",
            model_name="qwen-plus",
        )
    )

    assert model.model_name == "qwen-plus"
    assert str(model.openai_api_base) == "https://example.invalid/compatible-mode/v1"
