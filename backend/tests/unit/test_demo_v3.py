from typing import Any

import pytest

from scripts import demo_v3


def test_demo_completes_transaction_intent_without_exposing_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request(
        _opener: object,
        *,
        method: str,
        url: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append((method, url, payload))
        if url.endswith("/api/ready"):
            return {
                "database": "ok",
                "authentication": "configured",
                "model": "configured",
            }
        if url.endswith("/api/auth/register"):
            return {"id": "synthetic-buyer"}
        if url.endswith("/api/negotiations"):
            return {"session_id": 42, "created": True}
        if url.endswith("/messages"):
            return {"outcome": "OFFER_ACCEPTED", "formal_offer_id": 84}
        if url.endswith("/confirm"):
            return {
                "status": "AGREED",
                "confirmation_source": "AUTO_ACCEPTED_BUYER_OFFER",
            }
        raise AssertionError(url)

    monkeypatch.setattr(demo_v3, "_request_json", fake_request)
    result = demo_v3.run_demo(api_url="http://api.example/", product_id=1001)

    assert result == {
        "status": "ok",
        "user_id": "synthetic-buyer",
        "product_id": 1001,
        "session_id": 42,
        "confirmed_offer_id": 84,
        "confirmation_source": "AUTO_ACCEPTED_BUYER_OFFER",
        "business_boundary": "transaction_intent_only",
    }
    assert "password" not in result
    assert any(url.endswith("/confirm") for _, url, _ in calls)


def test_demo_requires_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        return {
            "database": "ok",
            "authentication": "configured",
            "model": "not_configured",
        }

    monkeypatch.setattr(demo_v3, "_request_json", fake_request)

    with pytest.raises(demo_v3.DemoError, match="模型尚未配置"):
        demo_v3.run_demo(api_url="http://api.example", product_id=1001)
