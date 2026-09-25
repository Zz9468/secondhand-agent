import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

PASSWORD_SCHEME = "scrypt"
PASSWORD_N = 2**14
PASSWORD_R = 8
PASSWORD_P = 1
PASSWORD_DKLEN = 32

IdentityKind = Literal["seller", "buyer"]
SELLER_SESSION_COOKIE = "secondhand_seller_session"
BUYER_SESSION_COOKIE = "secondhand_buyer_session"


class IdentityTokenError(ValueError):
    """身份令牌缺失、损坏、类型错误或已经过期。"""


@dataclass(frozen=True, slots=True)
class IdentityClaims:
    subject: str
    kind: IdentityKind
    expires_at: datetime


def hash_password(password: str) -> str:
    """使用带随机盐的 scrypt 保存密码，不保留可逆密码。"""

    _validate_password_length(password)
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=PASSWORD_N,
        r=PASSWORD_R,
        p=PASSWORD_P,
        dklen=PASSWORD_DKLEN,
    )
    return "$".join(
        (
            PASSWORD_SCHEME,
            str(PASSWORD_N),
            str(PASSWORD_R),
            str(PASSWORD_P),
            _encode(salt),
            _encode(derived),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    """验证密码；无效哈希和超长输入统一返回失败，避免泄漏内部错误。"""

    if not 1 <= len(password) <= 128:
        return False
    try:
        scheme, n_value, r_value, p_value, salt_value, digest_value = encoded.split(
            "$"
        )
        if scheme != PASSWORD_SCHEME:
            return False
        n = int(n_value)
        r = int(r_value)
        p = int(p_value)
        if (n, r, p) != (PASSWORD_N, PASSWORD_R, PASSWORD_P):
            return False
        salt = _decode(salt_value)
        expected = _decode(digest_value)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def create_identity_token(
    *,
    subject: str,
    kind: IdentityKind,
    secret: str,
    lifetime: timedelta,
    now: datetime | None = None,
) -> str:
    """签发只包含最小身份信息的 HMAC 令牌。"""

    if not subject or len(subject) > 64:
        raise ValueError("身份标识长度无效")
    if len(secret) < 32:
        raise ValueError("认证签名密钥至少需要 32 个字符")
    if lifetime.total_seconds() <= 0:
        raise ValueError("身份令牌有效期必须为正数")

    issued_at = _as_utc(now or datetime.now(UTC))
    expires_at = issued_at + lifetime
    payload = {
        "exp": int(expires_at.timestamp()),
        "iat": int(issued_at.timestamp()),
        "sub": subject,
        "typ": kind,
        "ver": 1,
    }
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded_payload = _encode(payload_bytes)
    signature = hmac.new(
        secret.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_encode(signature)}"


def decode_identity_token(
    token: str,
    *,
    expected_kind: IdentityKind,
    secret: str,
    now: datetime | None = None,
) -> IdentityClaims:
    """先验证签名，再校验令牌类型、时间和最小载荷。"""

    if len(secret) < 32:
        raise IdentityTokenError("认证服务未正确配置")
    try:
        encoded_payload, encoded_signature = token.split(".")
        supplied_signature = _decode(encoded_signature)
    except (ValueError, TypeError) as exc:
        raise IdentityTokenError("身份令牌格式无效") from exc

    expected_signature = hmac.new(
        secret.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise IdentityTokenError("身份令牌签名无效")

    try:
        payload = json.loads(_decode(encoded_payload))
        subject = payload["sub"]
        kind = payload["typ"]
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
        version = payload["ver"]
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise IdentityTokenError("身份令牌载荷无效") from exc

    current_timestamp = int(_as_utc(now or datetime.now(UTC)).timestamp())
    if (
        not isinstance(subject, str)
        or not subject
        or len(subject) > 64
        or kind != expected_kind
        or version != 1
        or issued_at > current_timestamp + 60
        or expires_at <= current_timestamp
        or expires_at <= issued_at
    ):
        raise IdentityTokenError("身份令牌已经失效")
    return IdentityClaims(
        subject=subject,
        kind=expected_kind,
        expires_at=datetime.fromtimestamp(expires_at, tz=UTC),
    )


def _validate_password_length(password: str) -> None:
    if not 12 <= len(password) <= 128:
        raise ValueError("密码长度必须在 12 到 128 个字符之间")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
