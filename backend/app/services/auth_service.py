from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password, verify_password
from app.db.models import UserAccount
from app.services.errors import UsernameAlreadyExistsError

# 不存在的账号仍执行一次相同算法，减少通过响应时间探测用户名的差异。
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-user-password")


@dataclass(frozen=True, slots=True)
class UserPrincipal:
    id: str
    username: str
    display_name: str


class AuthService:
    """注册并验证统一账号，不向 API 层暴露密码哈希。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def register_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
    ) -> UserPrincipal:
        normalized_username = username.strip().lower()
        try:
            with self._session_factory() as db, db.begin():
                account = UserAccount(
                    id=f"user-{uuid4().hex}",
                    username=normalized_username,
                    display_name=display_name,
                    password_hash=hash_password(password),
                    is_active=True,
                )
                db.add(account)
                db.flush()
                return self._principal(account)
        except IntegrityError as exc:
            # 数据库唯一约束是并发注册时的最终判定，不能把底层错误回显给客户端。
            raise UsernameAlreadyExistsError("用户名已被使用") from exc

    def authenticate_user(
        self,
        *,
        username: str,
        password: str,
    ) -> UserPrincipal | None:
        normalized_username = username.strip().lower()
        with self._session_factory() as db:
            account = db.scalar(
                select(UserAccount).where(
                    UserAccount.username == normalized_username
                )
            )
            encoded = (
                account.password_hash if account is not None else _DUMMY_PASSWORD_HASH
            )
            password_valid = verify_password(password, encoded)
            if account is None or not account.is_active or not password_valid:
                return None
            return self._principal(account)

    def get_active_user(self, user_id: str) -> UserPrincipal | None:
        with self._session_factory() as db:
            account = db.scalar(
                select(UserAccount).where(
                    UserAccount.id == user_id,
                    UserAccount.is_active.is_(True),
                )
            )
            if account is None:
                return None
            return self._principal(account)

    @staticmethod
    def _principal(account: UserAccount) -> UserPrincipal:
        return UserPrincipal(
            id=account.id,
            username=account.username,
            display_name=account.display_name,
        )
