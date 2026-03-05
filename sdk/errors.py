from __future__ import annotations

from typing import Any

import httpx


class FlashConnectorError(Exception):
    """Base SDK exception."""

    def __init__(self, message: str, *, status_code: int | None = None, detail: Any | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


class AuthenticationError(FlashConnectorError):
    """401 from API."""


class PermissionDeniedError(FlashConnectorError):
    """403 from API."""


class NotFoundError(FlashConnectorError):
    """404 from API."""


class ValidationError(FlashConnectorError):
    """400/422 from API."""


class ConflictError(FlashConnectorError):
    """409 from API."""


class RateLimitError(FlashConnectorError):
    """429 from API."""


class ServerError(FlashConnectorError):
    """5xx from API."""


class JobWaitTimeoutError(FlashConnectorError):
    """Raised when wait_for_job times out."""


class BatchWaitTimeoutError(FlashConnectorError):
    """Raised when wait_for_batch times out."""


def raise_for_response_error(response: httpx.Response) -> None:
    """Map non-2xx HTTP responses to typed SDK exceptions."""
    if not response.is_error:
        return

    detail: Any = None
    message = f"HTTP {response.status_code}"

    try:
        body = response.json()
    except ValueError:
        body = None

    if isinstance(body, dict):
        detail = body.get("detail", body)
    elif body is not None:
        detail = body
    else:
        detail = response.text

    if isinstance(detail, dict):
        if "message" in detail and isinstance(detail["message"], str):
            message = detail["message"]
        else:
            message = str(detail)
    elif isinstance(detail, str) and detail:
        message = detail

    kwargs = {"status_code": response.status_code, "detail": detail}
    status = response.status_code
    if status == 401:
        raise AuthenticationError(message, **kwargs)
    if status == 403:
        raise PermissionDeniedError(message, **kwargs)
    if status == 404:
        raise NotFoundError(message, **kwargs)
    if status in (400, 422):
        raise ValidationError(message, **kwargs)
    if status == 409:
        raise ConflictError(message, **kwargs)
    if status == 429:
        raise RateLimitError(message, **kwargs)
    if status >= 500:
        raise ServerError(message, **kwargs)
    raise FlashConnectorError(message, **kwargs)
