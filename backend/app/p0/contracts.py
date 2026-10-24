"""P0 最小夹具持续验证业务 Runner 共享的恢复入口判定。"""

from app.runtime.recovery import (  # noqa: F401
    Entry,
    RecoveryConflict,
    RunIdentity,
    can_confirm,
    choose_entry,
)
