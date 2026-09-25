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


class ShippingPayer(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"
