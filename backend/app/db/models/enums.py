from enum import StrEnum
from typing import TypeVar

from sqlalchemy import Enum

EnumType = TypeVar("EnumType", bound=StrEnum)


def stored_enum(enum_class: type[EnumType], *, name: str) -> Enum:
    """创建以枚举值落库、并由数据库约束取值范围的字符串枚举。"""

    values = [item.value for item in enum_class]
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [item.value for item in members],
        length=max(map(len, values)),
    )


class ProductStatus(StrEnum):
    DRAFT = "DRAFT"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class NegotiationStyle(StrEnum):
    FIRM = "FIRM"
    BALANCED = "BALANCED"
    FLEXIBLE = "FLEXIBLE"


class NegotiationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    AGREED = "AGREED"
    CLOSED = "CLOSED"


class MessageRole(StrEnum):
    BUYER = "BUYER"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


class OfferProposer(StrEnum):
    BUYER = "BUYER"
    AGENT = "AGENT"


class OfferStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class ApprovalFollowupStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class ModelTaskType(StrEnum):
    """需要在数据库事务外执行的模型任务类型。"""

    CHAT_DECISION = "CHAT_DECISION"
    APPROVAL_FOLLOWUP = "APPROVAL_FOLLOWUP"


class ModelTaskStatus(StrEnum):
    """持久化模型任务的生命周期状态。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STALE = "STALE"
    CANCELLED = "CANCELLED"


class ModelTaskErrorCategory(StrEnum):
    """不保存底层异常细节的稳定错误分类。"""

    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK = "NETWORK"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    BUSINESS_CONFLICT = "BUSINESS_CONFLICT"
    INTERNAL = "INTERNAL"
    UNKNOWN = "UNKNOWN"


class ConfirmationSource(StrEnum):
    """买家最终确认时经过后端复核的授权来源。"""

    AGENT_COUNTER = "AGENT_COUNTER"
    AUTO_ACCEPTED_BUYER_OFFER = "AUTO_ACCEPTED_BUYER_OFFER"
    SELLER_APPROVED_BUYER_OFFER = "SELLER_APPROVED_BUYER_OFFER"


class ShippingPayer(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"
