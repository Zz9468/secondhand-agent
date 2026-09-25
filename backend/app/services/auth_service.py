from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password, verify_password
from app.db.models import SellerAccount

# 不存在的账号仍执行一次相同算法，减少通过响应时间探测用户名的差异。
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-user-password")


@dataclass(frozen=True, slots=True)
class SellerPrincipal:
    id: str
    username: str


class AuthService:
    """从数据库验证卖家账号，不向 API 层暴露密码哈希。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def authenticate_seller(
        self,
        *,
        username: str,
        password: str,
    ) -> SellerPrincipal | None:
        normalized_username = username.strip().lower()
        with self._session_factory() as db:
            account = db.scalar(
                select(SellerAccount).where(
                    SellerAccount.username == normalized_username
                )
            )
            encoded = (
                account.password_hash if account is not None else _DUMMY_PASSWORD_HASH
            )
            password_valid = verify_password(password, encoded)
            if account is None or not account.is_active or not password_valid:
                return None
            return self._principal(account)

    def get_active_seller(self, seller_id: str) -> SellerPrincipal | None:
        with self._session_factory() as db:
            account = db.scalar(
                select(SellerAccount).where(
                    SellerAccount.id == seller_id,
                    SellerAccount.is_active.is_(True),
                )
            )
            if account is None:
                return None
            return self._principal(account)

    @staticmethod
    def _principal(account: SellerAccount) -> SellerPrincipal:
        return SellerPrincipal(id=account.id, username=account.username)
