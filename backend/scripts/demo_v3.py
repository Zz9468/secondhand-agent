"""通过公开 HTTP API 演示一次模式 B 的报价接受与交易意向确认闭环。"""

import argparse
import json
import secrets
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener


class DemoError(RuntimeError):
    pass


def _request_json(
    opener: object,
    *,
    method: str,
    url: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    data = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(request, timeout=15) as response:  # type: ignore[attr-defined]
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise DemoError(f"HTTP {exc.code}: {detail}") from exc


def run_demo(*, api_url: str, product_id: int) -> dict[str, object]:
    base_url = api_url.rstrip("/")
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    ready = _request_json(opener, method="GET", url=f"{base_url}/api/ready")
    if ready.get("database") != "ok":
        raise DemoError("数据库尚未就绪")
    if ready.get("authentication") != "configured":
        raise DemoError("AUTH_SECRET 尚未配置")
    if ready.get("model") != "configured":
        raise DemoError("模型尚未配置，聊天 API 会拒绝创建决策提供器")

    suffix = secrets.token_hex(6)
    username = f"v3-demo-{suffix}"
    password = f"V3-demo-{secrets.token_urlsafe(18)}-9"
    identity = _request_json(
        opener,
        method="POST",
        url=f"{base_url}/api/auth/register",
        payload={
            "username": username,
            "display_name": "V3 演示买家",
            "password": password,
        },
    )
    negotiation = _request_json(
        opener,
        method="POST",
        url=f"{base_url}/api/negotiations",
        payload={"product_id": product_id},
    )
    session_id = int(negotiation["session_id"])
    message = _request_json(
        opener,
        method="POST",
        url=f"{base_url}/api/negotiations/{session_id}/messages",
        payload={
            "request_id": f"demo-message-{suffix}",
            "content": "这是正式报价：2900 元，运费由买家承担。",
            "offer": {
                "price": "2900.00",
                "shipping_paid_by": "buyer",
                "shipping_cost": "0.00",
                "delivery_method": "shipping",
            },
        },
    )
    if message.get("outcome") != "OFFER_ACCEPTED":
        raise DemoError(f"报价没有进入自动接受终态: {message.get('outcome')}")
    offer_id = int(message["formal_offer_id"])
    confirmation = _request_json(
        opener,
        method="POST",
        url=f"{base_url}/api/negotiations/{session_id}/confirm",
        payload={
            "offer_id": offer_id,
            "request_id": f"demo-confirm-{suffix}",
        },
    )
    if confirmation.get("status") != "AGREED":
        raise DemoError("交易意向没有进入 AGREED")
    return {
        "status": "ok",
        "user_id": identity["id"],
        "product_id": product_id,
        "session_id": session_id,
        "confirmed_offer_id": offer_id,
        "confirmation_source": confirmation["confirmation_source"],
        "business_boundary": "transaction_intent_only",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--product-id", type=int, default=1001)
    args = parser.parse_args(argv)
    try:
        result = run_demo(api_url=args.api_url, product_id=args.product_id)
    except DemoError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
