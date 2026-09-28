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
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Correlation-ID"]


def test_request_observation_preserves_valid_ids_and_replaces_invalid_ones() -> None:
    request_id = "e0d2aa6a-a42a-45de-82b7-064f2cafba0f"
    response = client.get(
        "/api/health",
        headers={"X-Request-ID": request_id, "X-Correlation-ID": "unsafe"},
    )

    assert response.headers["X-Request-ID"] == request_id
    assert response.headers["X-Correlation-ID"] != "unsafe"


def test_ready_returns_database_status(monkeypatch) -> None:
    monkeypatch.setattr(health_module, "check_database_connection", lambda: None)
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        auth_secret="a-secure-test-secret-with-32-characters",
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
        "authentication": "configured",
    }


def test_ready_reports_missing_model_configuration(monkeypatch) -> None:
    monkeypatch.setattr(health_module, "check_database_connection", lambda: None)
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        auth_secret="a-secure-test-secret-with-32-characters",
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
        "authentication": "configured",
    }


def test_ready_reports_missing_authentication_configuration(monkeypatch) -> None:
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
        "status": "degraded",
        "database": "ok",
        "model": "configured",
        "authentication": "not_configured",
    }


def test_ready_hides_database_error_details(monkeypatch) -> None:
    def fail_connection() -> None:
        raise RuntimeError("secret connection details")

    monkeypatch.setattr(health_module, "check_database_connection", fail_connection)

    response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    assert "secret" not in response.text
