"""Shared schema base classes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """Base for every request/response payload.

    The wire format is camelCase to match the TypeScript frontend while Python
    code stays snake_case. ``populate_by_name`` means request bodies are
    accepted with either spelling, which keeps curl/scripts pleasant.
    ``from_attributes`` lets handlers return ORM objects directly.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
    )


class ApiMessage(ApiModel):
    """A plain confirmation payload for endpoints with nothing else to return."""

    message: str
    code: str | None = None
