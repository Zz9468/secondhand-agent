from fastapi.testclient import TestClient

from app.api import health as health_module
from app.core.config import Settings, get_settings
from app.main import app

client = TestClient(app)
TEST_DATABASE_URL = "mysql+pymysql://user:password@127.0.0.1:3306/test"


def test_health_returns_process_status() -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "SecondHand Agent API",
        "environment": "development",
    }


def test_ready_returns_database_status(monkeypatch) -> None:
    monkeypatch.setattr(health_module, "check_database_connection", lambda: None)
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        model_base_url="https://example.invalid/v1",
        model_api_key="test-key",
    )

    try:
        response = client.get("/api/ready")
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": "ok",
        "model": "configured",
    }


def test_ready_reports_missing_model_configuration(monkeypatch) -> None:
    monkeypatch.setattr(health_module, "check_database_connection", lambda: None)
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
    )

    try:
        response = client.get("/api/ready")
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "database": "ok",
        "model": "not_configured",
    }


def test_ready_hides_database_error_details(monkeypatch) -> None:
    def fail_connection() -> None:
        raise RuntimeError("secret connection details")

    monkeypatch.setattr(health_module, "check_database_connection", fail_connection)

    response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    assert "secret" not in response.text
