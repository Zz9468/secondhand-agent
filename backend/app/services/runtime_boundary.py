from app.services.errors import NegotiationLifecycleConflictError


def require_legacy_runtime(negotiation):
    """已绑定新版图的会话不得被旧入口绕过 FIFO、回执及展示校验。"""
    if negotiation.graph_version is not None:
        raise NegotiationLifecycleConflictError("此会话由新版图管理，请使用对应运行入口")
