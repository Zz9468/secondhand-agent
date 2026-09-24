from fastapi.testclient import TestClient

from app.api import health as health_module
from app.main import app

client = TestClient(app)


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

    response = client.get("/api/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


def test_ready_hides_database_error_details(monkeypatch) -> None:
    def fail_connection() -> None:
        raise RuntimeError("secret connection details")

    monkeypatch.setattr(health_module, "check_database_connection", fail_connection)

    response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    assert "secret" not in response.text

