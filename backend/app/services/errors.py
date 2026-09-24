class ServiceError(RuntimeError):
    """可安全转换为工具错误结果的业务异常。"""

    code = "SERVICE_ERROR"


class NegotiationNotFoundError(ServiceError):
    code = "NEGOTIATION_NOT_FOUND"


class ProductUnavailableError(ServiceError):
    code = "PRODUCT_UNAVAILABLE"


class PricingPolicyNotFoundError(ServiceError):
    code = "PRICING_POLICY_NOT_FOUND"


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
