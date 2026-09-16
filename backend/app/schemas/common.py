"""Shared response schemas: the error envelope and generic pagination."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.schemas.base import ApiModel


class ErrorBody(ApiModel):
    code: str = Field(description="Stable machine-readable error code")
    message: str = Field(description="Human readable error message")
    detail: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    """The single error envelope used by every endpoint."""

    error: ErrorBody


class PageMeta(ApiModel):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total: int = Field(ge=0)
    pages: int = Field(ge=0)


class Page[T](ApiModel):
    """Standard list envelope: ``{items: [...], meta: {...}}``."""

    items: list[T]
    meta: PageMeta


class PageParams(ApiModel):
    """Pagination query parameters.

    Camel-cased on the wire (``?page=1&pageSize=20``) like every other payload;
    the snake_case spelling keeps working for curl and scripts.
    """

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class SortParams(ApiModel):
    sort_by: str | None = None
    sort_desc: bool = False
