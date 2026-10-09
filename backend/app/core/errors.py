"""Application error types and the unified error envelope.

Every error returned by the API has the shape::

    {"error": {"code": "...", "message": "...", "detail": {...}}}

``code`` is a stable, machine-readable string; ``message`` is human readable;
``detail`` carries optional structured context (never a stack trace).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)

# Starlette renamed the 422 constant; read whichever name this version exposes
# without touching the deprecated attribute (which would emit a warning).
HTTP_422_UNPROCESSABLE: int = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)


class AppError(Exception):
    """Base class for all expected application errors."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "Internal server error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.detail = detail or {}
        self.status_code = status_code or self.status_code
        super().__init__(self.message)

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content={
                "error": {
                    "code": self.code,
                    "message": self.message,
                    "detail": self.detail,
                }
            },
        )


# --- 400 ---------------------------------------------------------------------
class BadRequestError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"
    message = "Bad request"


class ValidationFailedError(AppError):
    status_code = HTTP_422_UNPROCESSABLE
    code = "validation_failed"
    message = "Request validation failed"


# --- 401 / 403 ---------------------------------------------------------------
class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"
    message = "Authentication required"


class ForbiddenError(AppError):
    """The caller is authenticated but this action is not allowed for them."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"
    message = "Operation not permitted"


class PermissionDeniedError(ForbiddenError):
    """Specifically: a required permission code was missing."""

    code = "permission_denied"
    message = "You do not have permission to perform this action"


# --- 404 / 409 ---------------------------------------------------------------
class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Resource not found"


class TooManyRequestsError(AppError):
    """The caller has made too many attempts; see `app.auth.rate_limit`."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "Resource conflict"


class QuotaExceededError(AppError):
    """Quota is a *policy* failure, deliberately distinct from permission (403)."""

    status_code = status.HTTP_409_CONFLICT
    code = "quota_exceeded"
    message = "Quota exceeded"


# --- 502 / 503 ---------------------------------------------------------------
class ExternalServiceError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "external_service_error"
    message = "Upstream service error"


class KubernetesError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "kubernetes_error"
    message = "Kubernetes operation failed"


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "Service temporarily unavailable"


# --- Handlers ----------------------------------------------------------------
def _envelope(
    status_code: int, code: str, message: str, detail: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "detail": detail or {}}},
    )


def _serialisable_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """``exc.errors()`` made safe to put in a JSON body.

    Pydantic attaches the original exception object to ``ctx`` when a custom
    validator raises, and a ``ValueError`` is not JSON-serialisable. Passing the
    errors straight through therefore turned *every* ``field_validator`` that
    rejected a value into a 500 — the one case where a helpful 422 is most
    wanted, because the caller needs to be told which field was wrong.

    The exception itself is replaced with its message: the caller wants to read
    "镜像地址不能包含空格", not a repr of the object that carried it.
    """
    cleaned: list[dict[str, Any]] = []
    for error in exc.errors():
        entry = {key: value for key, value in error.items() if key != "ctx"}
        context = error.get("ctx")
        if isinstance(context, dict):
            entry["ctx"] = {
                key: (str(value) if isinstance(value, BaseException) else value)
                for key, value in context.items()
            }
        # `input` can be any object the client sent, including one Pydantic
        # parsed into a non-primitive; keep it only when it survives encoding.
        try:
            json.dumps(entry)
        except (TypeError, ValueError):
            entry.pop("input", None)
        cleaned.append(entry)
    return cleaned


#: Longest reason recorded on an audit row. The column is free text, but a reason
#: is meant to be read in a list, and an unbounded one invites dumping payloads.
_AUDIT_REASON_LIMIT = 500


def _field_error_summary(errors: list[dict[str, Any]]) -> str:
    """``name: String should match pattern ...`` — field path and rule.

    Deliberately built from ``loc`` and ``msg`` only. Pydantic also reports the
    rejected ``input``, and a request carrying a bad password is a 422 like any
    other; a rejection reason must never become a copy of what was submitted.
    """
    parts: list[str] = []
    for error in errors:
        location = [
            str(part) for part in (error.get("loc") or ()) if part not in ("body", "query", "path")
        ]
        parts.append(f"{'.'.join(location) or '<body>'}: {error.get('msg', '')}")
    return "; ".join(parts)


def _record_for_audit(request: Request, reason: str) -> None:
    """Hand the failure reason to ``AuditMiddleware`` for the audit row.

    The middleware reads ``scope['state']`` *after* the response has been sent,
    and exception handlers run inside it, so writing here is what fills in a row
    that would otherwise record ``status='error'`` with no indication of why.
    """
    request.state.error_message = reason[:_AUDIT_REASON_LIMIT]


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the unified error handlers to a FastAPI application."""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("app_error", code=exc.code, message=exc.message, detail=exc.detail)
        _record_for_audit(request, exc.message)
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = _serialisable_errors(exc)
        _record_for_audit(
            request,
            f"Request validation failed — {_field_error_summary(errors)}",
        )
        return _envelope(
            HTTP_422_UNPROCESSABLE,
            "validation_failed",
            "Request validation failed",
            {"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        _record_for_audit(request, str(exc.detail))
        code_map = {
            400: "bad_request",
            401: "unauthorized",
            403: "permission_denied",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
            429: "rate_limited",
        }
        return _envelope(
            exc.status_code,
            code_map.get(exc.status_code, "http_error"),
            str(exc.detail),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", error=str(exc))
        # The class name, never the message: a traceback is already logged, and an
        # exception message is the most likely place for credentials or internal
        # detail to reach a table that is merely admin-readable.
        _record_for_audit(request, f"unhandled {type(exc).__name__}")
        return _envelope(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "Internal server error",
        )
