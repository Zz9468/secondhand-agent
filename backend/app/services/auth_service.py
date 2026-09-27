import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password, verify_password
from app.db.models import UserAccount
from app.services.errors import NegotiationLifecycleConflictError

# 不存在的账号仍执行一次相同算法，减少通过响应时间探测用户名的差异。
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-user-password")
HISTORICAL_ACCOUNT_PASSWORD_HASH = "!HISTORICAL_VISITOR_NO_LOGIN!"


@dataclass(frozen=True, slots=True)
class SellerPrincipal:
    id: str
    username: str


class AuthService:
    """从统一账号表验证 V2 卖家登录，不向 API 层暴露密码哈希。"""

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

    def get_active_seller(self, seller_id: str) -> SellerPrincipal | None:
        with self._session_factory() as db:
            account = db.scalar(
                select(UserAccount).where(
                    UserAccount.id == seller_id,
                    UserAccount.is_active.is_(True),
                )
            )
            if account is None:
                return None
            return self._principal(account)

    @staticmethod
    def _principal(account: UserAccount) -> SellerPrincipal:
        return SellerPrincipal(id=account.id, username=account.username)


def ensure_historical_buyer_account(db: Session, *, buyer_id: str) -> UserAccount:
    """为 V2 访客建立独立的不可登录账号映射，供阶段一外键过渡使用。"""

    digest = hashlib.sha256(buyer_id.encode("utf-8")).hexdigest()
    statement = mysql_insert(UserAccount).values(
        id=buyer_id,
        username=f"history-{digest[:56]}",
        display_name=f"历史访客-{digest[:8]}",
        password_hash=HISTORICAL_ACCOUNT_PASSWORD_HASH,
        is_active=False,
    )
    # 同一访客可并发从不同商品创建会话；数据库原子地防止重复账号。
    # 冲突后仍读取并核对账号类型，不能把同名的真实账号当成访客复用。
    db.execute(statement.on_duplicate_key_update(id=UserAccount.id))
    account = db.get(UserAccount, buyer_id, with_for_update=True)
    if account is None or (
        account.is_active or account.password_hash != HISTORICAL_ACCOUNT_PASSWORD_HASH
    ):
        raise NegotiationLifecycleConflictError(
            "访客身份与现有统一账号冲突，不能错误合并身份"
        )
    return account
