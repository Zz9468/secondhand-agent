class ServiceError(RuntimeError):
    """可安全转换为工具错误结果的业务异常。"""

    code = "SERVICE_ERROR"


class NegotiationNotFoundError(ServiceError):
    code = "NEGOTIATION_NOT_FOUND"


class ProductUnavailableError(ServiceError):
    code = "PRODUCT_UNAVAILABLE"


class ProductNotFoundError(ServiceError):
    code = "PRODUCT_NOT_FOUND"


class PricingPolicyNotFoundError(ServiceError):
    code = "PRICING_POLICY_NOT_FOUND"


class PolicyVersionConflictError(ServiceError):
    code = "POLICY_VERSION_CONFLICT"


class ApprovalNotFoundError(ServiceError):
    code = "APPROVAL_NOT_FOUND"


class ApprovalConflictError(ServiceError):
    code = "APPROVAL_CONFLICT"


class ApprovalNotAuthorizedError(ServiceError):
    code = "APPROVAL_NOT_AUTHORIZED"


class ApprovalNotExpiredError(ServiceError):
    code = "APPROVAL_NOT_EXPIRED"


class InvalidNegotiationStateError(ServiceError):
    code = "INVALID_NEGOTIATION_STATE"


class InvalidOfferTermsError(ServiceError):
    code = "INVALID_OFFER_TERMS"


class OfferNotFoundError(ServiceError):
    code = "OFFER_NOT_FOUND"


class OfferConflictError(ServiceError):
    code = "OFFER_CONFLICT"


class OfferNotAuthorizedError(ServiceError):
    code = "OFFER_NOT_AUTHORIZED"


class MessageConflictError(ServiceError):
    code = "MESSAGE_CONFLICT"


class IncompleteRequestError(ServiceError):
    code = "INCOMPLETE_REQUEST"
