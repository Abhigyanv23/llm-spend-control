class GatewayError(Exception):
    """Base error. Every error the client sees has the same shape.

    `audit_status` is the value written to request_logs.status when this error ends a request.
    """
    audit_status = "internal_error"

    def __init__(self, message: str, status_code: int = 500,
                 code: str = "gateway_error", retryable: bool = False,
                 extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        self.extra = extra or {}

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message,
                          "retryable": self.retryable, **self.extra}}


class UnknownModelError(GatewayError):
    audit_status = "validation_error"

    def __init__(self, model: str):
        super().__init__(f"Model '{model}' is not in the registry",
                         status_code=400, code="unknown_model")


class ContextTooLongError(GatewayError):
    audit_status = "validation_error"

    def __init__(self, model: str, estimated: int, limit: int):
        super().__init__(
            f"Estimated {estimated} input tokens exceeds {model}'s limit of {limit}",
            status_code=400, code="context_too_long")


class ProviderError(GatewayError):
    audit_status = "provider_error"

    def __init__(self, provider: str, message: str, status_code: int = 502,
                 retryable: bool = False):
        super().__init__(message, status_code=status_code, code="provider_error",
                         retryable=retryable, extra={"provider": provider})


class NoRouteError(GatewayError):
    """503: no available model satisfies the request's routing constraints
    (e.g. required capabilities, or no API key for any candidate in the allowed tiers)."""
    audit_status = "provider_error"

    def __init__(self, message: str):
        super().__init__(message, status_code=503, code="no_route")


class BudgetExceededError(GatewayError):
    """402: the request would push a team/feature past a limit and cannot be overridden."""
    audit_status = "budget_blocked"

    def __init__(self, message: str, details: dict):
        # Not retryable: retrying before the reset time will fail the same way
        super().__init__(message, status_code=402, code="budget_exceeded", extra=details)


class OverrideRequiredError(GatewayError):
    """402: a high/critical request hit a limit; it may proceed with X-Budget-Override."""
    audit_status = "budget_blocked"

    def __init__(self, message: str, details: dict):
        super().__init__(message, status_code=402, code="override_required", extra=details)


class BudgetUnavailableError(GatewayError):
    """503: budgets cannot be checked (Redis/Postgres down) and BUDGET_FAIL_MODE=closed."""
    audit_status = "budget_blocked"

    def __init__(self, reason: str):
        super().__init__(
            f"Budget service unavailable ({reason}); requests are rejected while "
            f"BUDGET_FAIL_MODE=closed", status_code=503, code="budget_unavailable",
            retryable=True)
