import pytest
from pydantic import ValidationError

from app.schemas.auth import UserLoginRequest, UserRegisterRequest


def test_auth_schema_normalizes_username_without_changing_password() -> None:
    payload = UserLoginRequest(
        username="  Account_01  ",
        password=" password-with-spaces ",
    )

    assert payload.username == "account_01"
    assert payload.password == " password-with-spaces "


def test_register_schema_normalizes_safe_display_name() -> None:
    payload = UserRegisterRequest(
        username="member-01",
        display_name="  Ａlice 用户  ",
        password="Strong-password-2026",
    )

    assert payload.display_name == "Alice 用户"


@pytest.mark.parametrize(
    ("display_name", "password"),
    (
        ("用户\n名称", "Strong-password-2026"),
        ("测试用户", "onlyletterspassword"),
    ),
)
def test_register_schema_rejects_unsafe_public_name_or_weak_password(
    display_name: str,
    password: str,
) -> None:
    with pytest.raises(ValidationError):
        UserRegisterRequest(
            username="member-01",
            display_name=display_name,
            password=password,
        )
