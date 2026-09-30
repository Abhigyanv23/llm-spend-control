class GatewayError(Exception):
    """Base error. Every error the client sees has the same shape."""

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
    def __init__(self, model: str):
        super().__init__(f"Model '{model}' is not in the registry",
                         status_code=400, code="unknown_model")


class ContextTooLongError(GatewayError):
    def __init__(self, model: str, estimated: int, limit: int):
        super().__init__(
            f"Estimated {estimated} input tokens exceeds {model}'s limit of {limit}",
            status_code=400, code="context_too_long")


class ProviderError(GatewayError):
    def __init__(self, provider: str, message: str, status_code: int = 502,
                 retryable: bool = False):
        super().__init__(message, status_code=status_code, code="provider_error",
                         retryable=retryable, extra={"provider": provider})