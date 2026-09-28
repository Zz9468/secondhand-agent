class ServiceError(RuntimeError):
    """可安全转换为工具错误结果的业务异常。"""

    code = "SERVICE_ERROR"


class UsernameAlreadyExistsError(ServiceError):
    code = "USERNAME_ALREADY_EXISTS"


class NegotiationNotFoundError(ServiceError):
    code = "NEGOTIATION_NOT_FOUND"


class ProductUnavailableError(ServiceError):
    code = "PRODUCT_UNAVAILABLE"


class ProductNotFoundError(ServiceError):
    code = "PRODUCT_NOT_FOUND"


class ProductDeletionConflictError(ServiceError):
    code = "PRODUCT_DELETION_CONFLICT"


class SellerNotFoundError(ServiceError):
    code = "SELLER_NOT_FOUND"


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


class ModelDecisionError(ServiceError):
    code = "MODEL_DECISION_ERROR"


class ModelTaskRecoveryRequiredError(ServiceError):
    code = "MODEL_TASK_RECOVERY_REQUIRED"


class ModelExecutionTaskNotFoundError(ServiceError):
    code = "MODEL_EXECUTION_TASK_NOT_FOUND"


class ModelExecutionTaskConflictError(ServiceError):
    code = "MODEL_EXECUTION_TASK_CONFLICT"


class ModelExecutionTaskLeaseError(ServiceError):
    code = "MODEL_EXECUTION_TASK_LEASE_ERROR"


class InvalidModelExecutionTaskError(ServiceError):
    code = "INVALID_MODEL_EXECUTION_TASK"


class NegotiationLifecycleConflictError(ServiceError):
    code = "NEGOTIATION_LIFECYCLE_CONFLICT"
