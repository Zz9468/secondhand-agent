import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.security import (
    IdentityTokenError,
    create_identity_token,
    decode_identity_token,
    hash_password,
    verify_password,
)

TEST_SECRET = "a-secure-test-secret-with-32-characters"


def test_password_hash_is_salted_and_verifiable() -> None:
    first = hash_password("correct-horse-battery")
    second = hash_password("correct-horse-battery")

    assert first != second
    assert "correct-horse-battery" not in first
    assert verify_password("correct-horse-battery", first) is True
    assert verify_password("wrong-password-value", first) is False
    assert verify_password("correct-horse-battery", "invalid-hash") is False


def test_password_hash_rejects_unsafe_lengths() -> None:
    with pytest.raises(ValueError, match="12"):
        hash_password("short")
    with pytest.raises(ValueError, match="128"):
        hash_password("x" * 129)


def test_identity_token_binds_type_signature_and_expiration() -> None:
    now = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    token = create_identity_token(
        subject="user-123",
        secret=TEST_SECRET,
        lifetime=timedelta(minutes=30),
        now=now,
    )

    claims = decode_identity_token(
        token,
        secret=TEST_SECRET,
        now=now + timedelta(minutes=1),
    )
    assert claims.subject == "user-123"

    with pytest.raises(IdentityTokenError):
        decode_identity_token(
            f"{token[:-1]}x",
            secret=TEST_SECRET,
            now=now,
        )
    with pytest.raises(IdentityTokenError):
        decode_identity_token(
            token,
            secret=TEST_SECRET,
            now=now + timedelta(minutes=31),
        )

    with pytest.raises(IdentityTokenError):
        decode_identity_token(
            _legacy_token(subject="user-123", kind="seller", now=now),
            secret=TEST_SECRET,
            now=now + timedelta(minutes=1),
        )


def _legacy_token(*, subject: str, kind: str, now: datetime) -> str:
    """仅为回归测试构造已停止签发的 V2 身份类型令牌。"""

    payload = {
        "exp": int((now + timedelta(minutes=30)).timestamp()),
        "iat": int(now.timestamp()),
        "sub": subject,
        "typ": kind,
        "ver": 1,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        TEST_SECRET.encode(),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{encoded}.{encoded_signature}"
