"""Sanitized errors raised by the NeuralTrust components."""

from copy import deepcopy
from typing import Any


class NeuralTrustError(RuntimeError):
    """Base error; request bodies, credentials and raw server errors are never included."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NeuralTrustAuthenticationError(NeuralTrustError):
    """The API credential is missing, invalid, or unauthorized."""


class NeuralTrustUnavailableError(NeuralTrustError):
    """A retryable evaluation failure exhausted the configured retry budget."""


class NeuralTrustRequestError(NeuralTrustError):
    """TrustGuard rejected the request or a non-retryable transport failure occurred."""


class NeuralTrustInvalidResponseError(NeuralTrustError):
    """The response or transformation cannot be safely interpreted."""


class NeuralTrustBlockedError(NeuralTrustError):
    """Evaluation blocked content or requested approval that this component cannot grant.

    ``status`` preserves ``block`` or ``ask``. ``verdict`` contains only that status
    and validated correlation IDs, never findings, original content or transforms.
    """

    def __init__(self, verdict: dict[str, Any]) -> None:
        self.status = verdict["status"]
        self.verdict = deepcopy({key: verdict[key] for key in ("status", "trace_id", "request_id") if key in verdict})
        super().__init__(f"TrustGuard stopped content with status '{self.status}'.")
